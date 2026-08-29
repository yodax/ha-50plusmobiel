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

from datetime import date, timedelta

import aiohttp
import pytest

from custom_components.mobiel50plus.api import (
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
    """Stands in for an aiohttp `ClientResponse` used as an async context manager."""

    def __init__(self, *, status: int = 200, json_data: object = None) -> None:
        self.status = status
        self._json_data = {} if json_data is None else json_data

    async def json(self) -> object:
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
            "bundle_refresh_date": date.today() + timedelta(days=18),
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
