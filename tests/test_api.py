"""Unit tests for Mobiel50PlusApiClient — no Home Assistant needed here.

Requests are faked with a small local `FakeSession`/`FakeResponse` pair
instead of a third-party HTTP-mocking library: aioresponses (as of 0.7.9)
doesn't support the aiohttp version Home Assistant itself pins, and the
class under test only calls `session.post(...)` as an async context
manager plus `.status`/`.json()`/`.raise_for_status()` on the result, which
is trivial and more robust to fake directly than to fight a mocking
library's version compatibility.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.mobiel50plus.api import (
    ACCOUNT_NAME_QUERY,
    GRAPHQL_URL,
    TOKEN_URL,
    VERIFY_LOGIN_URL,
    Mobiel50PlusApiClient,
    Mobiel50PlusApiError,
    Mobiel50PlusAuthError,
)

USERNAME = "user@example.com"
PASSWORD = "hunter2"

GOOD_STATUS_PAYLOAD = {
    "data": {
        "me": {
            "subscriptionGroups": [
                {
                    "remainingBeforeBill": 18,
                    "activeContract": {"endDate": "2027-01-01"},
                    "msisdns": [
                        {
                            "balance": {
                                "voiceAvailable": None,
                                "smsAvailable": None,
                                "dataAvailable": 9433,
                                "dataAssigned": 12000,
                                "dataPercentage": 79,
                            }
                        }
                    ],
                }
            ]
        }
    }
}


class FakeResponse:
    """Stands in for an aiohttp `ClientResponse` used as an async context manager.

    `raises` injects an exception from `.json()`, which the fake could not do
    before: an independent review pointed out that the fake always decoded
    successfully, so a method documented as never raising could still leak a
    `ValueError` or a `TimeoutError` past its own tests.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        json_data: object = None,
        raises: BaseException | None = None,
    ) -> None:
        self.status = status
        self._json_data = {} if json_data is None else json_data
        self._raises = raises

    async def json(self) -> object:
        if self._raises is not None:
            raise self._raises
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status
            )

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeSession:
    """Replays canned responses in call order; records what was requested."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def verify_login_responses(
    *, second_step: str = "login", message: str | None = None
) -> list[FakeResponse]:
    """The two-step verifyLogin handshake api.py always drives."""
    second_payload: dict = {"step": second_step}
    if message:
        second_payload["message"] = message
    return [
        FakeResponse(json_data={"step": "password"}),
        FakeResponse(json_data=second_payload),
    ]


def oauth_response(*, status: int = 200, access_token: str = "tok-123") -> FakeResponse:
    if status == 401:
        return FakeResponse(status=401)
    return FakeResponse(
        json_data={"access_token": access_token, "refresh_token": "refresh-1"}
    )


def full_login_responses() -> list[FakeResponse]:
    return [*verify_login_responses(), oauth_response()]


def make_client(responses: list[FakeResponse]) -> tuple[Mobiel50PlusApiClient, FakeSession]:
    session = FakeSession(responses)
    return Mobiel50PlusApiClient(session, USERNAME, PASSWORD), session


class TestAsyncLogin:
    async def test_happy_path_stores_access_token(self) -> None:
        client, session = make_client(full_login_responses())

        await client.async_login()

        assert client._access_token == "tok-123"
        assert [call[0] for call in session.calls] == [
            VERIFY_LOGIN_URL,
            VERIFY_LOGIN_URL,
            TOKEN_URL,
        ]

    async def test_rejected_credentials_raise_auth_error(self) -> None:
        client, _session = make_client(
            [
                FakeResponse(json_data={"step": "password"}),
                FakeResponse(
                    json_data={"message": "Ongeldige inloggegevens", "step": "password"}
                ),
            ]
        )

        with pytest.raises(Mobiel50PlusAuthError, match="Ongeldige inloggegevens"):
            await client.async_login()

    async def test_unsupported_auth_factor_raises_api_error_not_auth_error(self) -> None:
        """A step like "phone"/"recaptcha" isn't a credentials problem — reauth can't fix it."""
        client, _session = make_client(verify_login_responses(second_step="phone"))

        with pytest.raises(Mobiel50PlusApiError, match="phone"):
            await client.async_login()

    async def test_oauth_401_raises_auth_error(self) -> None:
        client, _session = make_client(
            [*verify_login_responses(), oauth_response(status=401)]
        )

        with pytest.raises(Mobiel50PlusAuthError):
            await client.async_login()

    async def test_oauth_missing_access_token_raises_auth_error(self) -> None:
        client, _session = make_client(
            [
                *verify_login_responses(),
                FakeResponse(
                    json_data={"error": "invalid_grant", "error_description": "bad creds"}
                ),
            ]
        )

        with pytest.raises(Mobiel50PlusAuthError, match="bad creds"):
            await client.async_login()


class TestAsyncGetStatus:
    async def test_happy_path(self) -> None:
        client, session = make_client(
            [*full_login_responses(), FakeResponse(json_data=GOOD_STATUS_PAYLOAD)]
        )

        result = await client.async_get_status()

        assert result == {
            "remaining_mb": 9433,
            "bundle_size_mb": 12000,
            "remaining_minutes": None,
            "remaining_sms": None,
            "data_percentage": 79,
            "days_to_bundle_refresh": 18,
            # Home Assistant's timezone, not the process's — see
            # TestBundleRefreshDateTimezone for the edge this protects.
            "bundle_refresh_date": dt_util.now().date() + timedelta(days=18),
            "contract_end_date": date(2027, 1, 1),
        }
        assert session.calls[-1][0] == GRAPHQL_URL

    async def test_no_active_contract_or_refresh_date_is_none(self) -> None:
        payload = {
            "data": {
                "me": {
                    "subscriptionGroups": [
                        {
                            "msisdns": [
                                {
                                    "balance": {
                                        "voiceAvailable": 100,
                                        "smsAvailable": 50,
                                        "dataAvailable": 1,
                                        "dataAssigned": 1,
                                        "dataPercentage": 1,
                                    }
                                }
                            ]
                        }
                    ]
                }
            }
        }
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=payload)]
        )

        result = await client.async_get_status()

        assert result["days_to_bundle_refresh"] is None
        assert result["bundle_refresh_date"] is None
        assert result["contract_end_date"] is None

    async def test_graphql_errors_array_raises_api_error(self) -> None:
        client, _session = make_client(
            [
                *full_login_responses(),
                FakeResponse(
                    json_data={"errors": [{"message": "field not found"}], "data": None}
                ),
            ]
        )

        with pytest.raises(Mobiel50PlusApiError, match="field not found"):
            await client.async_get_status()

    async def test_graphql_errors_with_non_dict_entry(self) -> None:
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data={"errors": ["rate limited"]})]
        )

        with pytest.raises(Mobiel50PlusApiError, match="rate limited"):
            await client.async_get_status()

    async def test_expired_token_reauths_and_retries(self) -> None:
        client, session = make_client(
            [
                *full_login_responses(),
                FakeResponse(status=401),
                *full_login_responses(),
                FakeResponse(json_data=GOOD_STATUS_PAYLOAD),
            ]
        )

        result = await client.async_get_status()

        assert result["remaining_mb"] == 9433
        # verifyLogin x2 + oauth, graphql(401), verifyLogin x2 + oauth, graphql
        assert len(session.calls) == 8

    async def test_still_401_after_reauth_raises_api_error_not_auth_error(self) -> None:
        """Login just succeeded, so this can't be a credentials problem."""
        client, _session = make_client(
            [
                *full_login_responses(),
                FakeResponse(status=401),
                *full_login_responses(),
                FakeResponse(status=401),
            ]
        )

        with pytest.raises(Mobiel50PlusApiError, match="after re-authenticating"):
            await client.async_get_status()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="empty-payload"),
            pytest.param({"data": {}}, id="no-me"),
            pytest.param(
                {"data": {"me": {"subscriptionGroups": []}}}, id="no-subscription-groups"
            ),
            pytest.param(
                {"data": {"me": {"subscriptionGroups": [{"msisdns": []}]}}},
                id="no-msisdns",
            ),
            pytest.param(
                {
                    "data": {
                        "me": {
                            "subscriptionGroups": [{"msisdns": [{"balance": None}]}]
                        }
                    }
                },
                id="null-balance",
            ),
        ],
    )
    async def test_malformed_shape_raises_api_error(self, payload: dict) -> None:
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=payload)]
        )

        with pytest.raises(Mobiel50PlusApiError, match="Unexpected response shape"):
            await client.async_get_status()

    async def test_invalid_end_date_raises_api_error(self) -> None:
        payload = {
            "data": {
                "me": {
                    "subscriptionGroups": [
                        {
                            "activeContract": {"endDate": "not-a-date"},
                            "msisdns": [
                                {
                                    "balance": {
                                        "voiceAvailable": None,
                                        "smsAvailable": None,
                                        "dataAvailable": 1,
                                        "dataAssigned": 1,
                                        "dataPercentage": 1,
                                    }
                                }
                            ],
                        }
                    ]
                }
            }
        }
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=payload)]
        )

        with pytest.raises(Mobiel50PlusApiError, match="Unexpected response shape"):
            await client.async_get_status()

    async def test_non_numeric_remaining_before_bill_raises_api_error(self) -> None:
        payload = {
            "data": {
                "me": {
                    "subscriptionGroups": [
                        {
                            "remainingBeforeBill": "not-a-number",
                            "msisdns": [
                                {
                                    "balance": {
                                        "voiceAvailable": None,
                                        "smsAvailable": None,
                                        "dataAvailable": 1,
                                        "dataAssigned": 1,
                                        "dataPercentage": 1,
                                    }
                                }
                            ],
                        }
                    ]
                }
            }
        }
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=payload)]
        )

        with pytest.raises(Mobiel50PlusApiError, match="Unexpected response shape"):
            await client.async_get_status()


class TestBundleRefreshDateTimezone:
    """`bundle_refresh_date` must be derived from Home Assistant's configured
    timezone, not from whatever timezone the container process happens to run in.

    FAILING-FIRST: against the pre-fix `date.today()` implementation,
    `test_uses_ha_timezone_not_process_timezone` returned date(2026, 2, 2) —
    one day early — because at 23:30 UTC the process (TZ=UTC) is still on the
    15th while Home Assistant's configured Europe/Amsterdam is already on the
    16th. Verified failing before the fix landed.
    """

    @pytest.fixture
    def utc_process_clock(self):
        """Pin the *process* clock to UTC, whatever this machine is set to.

        Restores TZ by hand rather than via monkeypatch: monkeypatch undoes its
        setenv *after* this fixture's teardown, so a `tzset()` here ran while TZ
        was still "UTC" and left libc pinned to UTC for the rest of the session
        on any non-UTC runner. The environment has to be put back first.
        """
        original = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            yield
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    async def test_uses_ha_timezone_not_process_timezone(
        self, hass: HomeAssistant, utc_process_clock: None
    ) -> None:
        # Home Assistant's own timezone, deliberately not the process's. Set
        # through hass so the test harness restores it (writing
        # dt_util.DEFAULT_TIME_ZONE directly leaks into later tests).
        await hass.config.async_set_time_zone("Europe/Amsterdam")
        assert dt_util.DEFAULT_TIME_ZONE == ZoneInfo("Europe/Amsterdam")

        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=GOOD_STATUS_PAYLOAD)]
        )

        # 23:30 UTC on the 15th is already 00:30 on the 16th in Amsterdam.
        with freeze_time("2026-01-15 23:30:00"):
            assert date.today() == date(2026, 1, 15)  # the process disagrees
            result = await client.async_get_status()

        # 16 Jan + 18 days = 3 Feb. Via date.today() it would be 2 Feb.
        assert result["bundle_refresh_date"] == date(2026, 2, 3)

    async def test_matches_process_date_when_timezones_agree(
        self, hass: HomeAssistant, utc_process_clock: None
    ) -> None:
        """Sanity check on the other side of the edge — same day, same answer."""
        await hass.config.async_set_time_zone("UTC")
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=GOOD_STATUS_PAYLOAD)]
        )

        with freeze_time("2026-01-15 23:30:00"):
            result = await client.async_get_status()

        assert result["bundle_refresh_date"] == date(2026, 2, 2)


class TestAsyncGetAccountFirstName:
    """The naming lookup used by the config flow.

    FAILING-FIRST: every test in this class failed with ImportError /
    AttributeError before `ACCOUNT_NAME_QUERY` and
    `async_get_account_first_name()` existed.
    """

    async def test_returns_only_the_first_name(self) -> None:
        """The API returns a whole Customer object; only one field comes back."""
        client, session = make_client(
            [
                *full_login_responses(),
                FakeResponse(
                    json_data={"data": {"me": {"firstName": "Sam"}}}
                ),
            ]
        )

        assert await client.async_get_account_first_name() == "Sam"
        # The query itself asks for nothing but the first name — no lastName,
        # no email, no iban, no invoiceAddress.
        sent_query = session.calls[-1][1]["json"]["query"]
        assert sent_query == ACCOUNT_NAME_QUERY
        for field in ("lastName", "email", "iban", "invoiceAddress", "msisdn"):
            assert field not in sent_query

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"data": {"me": {"firstName": None}}}, id="null-name"),
            pytest.param({"data": {"me": {"firstName": "   "}}}, id="blank-name"),
            pytest.param({"data": {"me": {}}}, id="field-absent"),
            pytest.param({"data": {"me": None}}, id="null-me"),
            pytest.param({"data": None}, id="null-data"),
            pytest.param({}, id="empty-payload"),
            pytest.param(
                {"errors": [{"message": "Cannot query field firstName"}]},
                id="graphql-error",
            ),
            pytest.param({"data": {"me": {"firstName": 42}}}, id="non-string"),
        ],
    )
    async def test_unusable_answers_return_none_rather_than_raising(
        self, payload: dict
    ) -> None:
        """A missing display name must never block adding the integration."""
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(json_data=payload)]
        )

        assert await client.async_get_account_first_name() is None

    async def test_expired_token_returns_none_without_crashing(self) -> None:
        client, _session = make_client([*full_login_responses(), FakeResponse(status=401)])

        assert await client.async_get_account_first_name() is None

    async def test_logs_in_first_when_no_token_yet(self) -> None:
        client, session = make_client(
            [*full_login_responses(), FakeResponse(json_data={"data": {"me": {"firstName": "Dean"}}})]
        )

        assert await client.async_get_account_first_name() == "Dean"
        assert [call[0] for call in session.calls] == [
            VERIFY_LOGIN_URL,
            VERIFY_LOGIN_URL,
            TOKEN_URL,
            GRAPHQL_URL,
        ]


class TestRequestsAreShapedCorrectly:
    """The fake answers any request, so what was *sent* needs asserting directly."""

    async def test_status_query_carries_the_bearer_token(self) -> None:
        client, session = make_client(
            [*full_login_responses(), FakeResponse(json_data=GOOD_STATUS_PAYLOAD)]
        )

        await client.async_get_status()

        url, kwargs = session.calls[-1]
        assert url == GRAPHQL_URL
        assert kwargs["headers"] == {"Authorization": "Bearer tok-123"}
        assert kwargs["json"]["operationName"] == "getCustomerForMsisdn"

    async def test_name_query_carries_the_bearer_token(self) -> None:
        client, session = make_client(
            [
                *full_login_responses(),
                FakeResponse(json_data={"data": {"me": {"firstName": "Sam"}}}),
            ]
        )

        await client.async_get_account_first_name()

        _url, kwargs = session.calls[-1]
        assert kwargs["headers"] == {"Authorization": "Bearer tok-123"}
        assert kwargs["json"]["operationName"] == "getCustomerName"

    async def test_login_never_sends_the_password_in_the_first_step(self) -> None:
        client, session = make_client(full_login_responses())

        await client.async_login()

        first_verify = session.calls[0][1]["json"]
        assert first_verify["password"] is None
        assert first_verify["step"] is None
        assert session.calls[1][1]["json"]["step"] == "password"


class TestAccountNameLookupReallyNeverRaises:
    """FAILING-FIRST: with the narrower `except (aiohttp.ClientError, ...)` this
    method used to have, both of these propagated out of a method whose
    docstring promised it never raises. Verified failing before the fix.
    """

    @pytest.mark.parametrize(
        "raises",
        [
            pytest.param(TimeoutError("timed out"), id="timeout"),
            pytest.param(ValueError("not json"), id="json-decode-error"),
            pytest.param(aiohttp.ClientError("connection reset"), id="client-error"),
            pytest.param(RuntimeError("something else entirely"), id="unexpected"),
        ],
    )
    async def test_returns_none_instead_of_raising(self, raises: BaseException) -> None:
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(raises=raises)]
        )

        assert await client.async_get_account_first_name() is None

    async def test_a_failed_login_also_returns_none(self) -> None:
        client, _session = make_client(
            [
                FakeResponse(json_data={"step": "password"}),
                FakeResponse(json_data={"message": "Ongeldige inloggegevens"}),
            ]
        )

        assert await client.async_get_account_first_name() is None


class TestStatusStillRaisesOnUnusableResponses:
    """The broad `except` on the *name* lookup must not have been copied onto
    the status path, where a silent None would mean silently wrong sensors."""

    @pytest.mark.parametrize(
        "raises",
        [
            pytest.param(ValueError("not json"), id="json-decode-error"),
            pytest.param(TimeoutError("timed out"), id="timeout"),
        ],
    )
    async def test_decode_and_timeout_errors_propagate(
        self, raises: BaseException
    ) -> None:
        client, _session = make_client(
            [*full_login_responses(), FakeResponse(raises=raises)]
        )

        with pytest.raises(type(raises)):
            await client.async_get_status()
