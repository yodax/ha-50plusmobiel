"""Client for the (unofficial) mijn.50plusmobiel.nl API.

mijn.50plusmobiel.nl is a client-rendered SPA backed by a GraphQL API. The
portal was reworked at some point after this client was first written,
retiring the old standalone `/token/login` endpoint (see CLAUDE.md) in
favour of a login flow that actually requires the `verifyLogin` handshake
the SPA form drives. This client now replicates that handshake.

Endpoints (captured via devtools against a real account, see CLAUDE.md):

- ``POST /verifyLogin`` — called twice: once with just ``username`` (other
  fields ``null``/empty) to discover the required next step, then again
  with ``password`` and ``step: "password"`` to actually validate
  credentials. Response is ``{"step": ...}`` — `"login"` means credentials
  were accepted and the SPA proceeds to `/oauth/token`; any other step
  (`"phone"`, `"recaptcha"`, `"redirect"`, ...) means an auth factor this
  client doesn't implement is required. On bad credentials the response
  carries a human-readable (Dutch) ``message`` field instead. Stateless —
  no cookies are set or required between calls.
- ``POST /oauth/token`` — body ``{"grant_type": "password", "client_id":
  ..., "scope": "CUSTOMER", "username": ..., "password": ...}``. On success
  returns ``{"access_token": ..., "refresh_token": ...}``; on failure
  returns HTTP 401 with an empty body (unlike the old `/token/login`, the
  status code *does* distinguish success/failure here, but there's no JSON
  error body to parse). ``client_id`` is a public value read out of the
  SPA's own JS bundle, not a secret.
- ``POST /api/graphql`` — GraphQL endpoint, ``Authorization: Bearer
  <access_token>``. The ``getCustomerForMsisdn`` query with no
  ``selectedMsisdn`` variable returns the account's own number(s) by
  default (no need to look up msisdns separately).

The live schema (confirmed via GraphQL introspection, which is enabled on
this endpoint) exposes more than the SPA's own query uses:
``Balance.dataPercentage`` gives remaining-data percentage directly (no
need to compute it from dataAvailable/dataAssigned), ``SubscriptionGroup
.remainingBeforeBill`` gives days until the data bundle resets, and
``SubscriptionGroup.activeContract.endDate`` gives the contract end date.
All three are included in ``STATUS_QUERY`` below.
"""
from __future__ import annotations

from datetime import date, timedelta

import aiohttp

API_BASE = "https://mijn.50plusmobiel.nl"
VERIFY_LOGIN_URL = f"{API_BASE}/verifyLogin"
TOKEN_URL = f"{API_BASE}/oauth/token"
GRAPHQL_URL = f"{API_BASE}/api/graphql"

# Public OAuth client id the SPA itself uses for the password grant (read out
# of its JS bundle) — not a secret, just an API-consumer identifier.
OAUTH_CLIENT_ID = "e80df68638c3ba8264d0d762da442ee0"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# dataPercentage, remainingBeforeBill and activeContract.endDate aren't used
# by the SPA's own getCustomerForMsisdn call but exist on the live schema
# (confirmed via GraphQL introspection against a real account, see CLAUDE.md) —
# added here to avoid computing percentage/reset-date client-side and to
# surface a contract end date.
STATUS_QUERY = """
query getCustomerForMsisdn {
  me {
    subscriptionGroups {
      remainingBeforeBill
      activeContract {
        endDate
      }
      msisdns {
        balance {
          voiceAvailable
          smsAvailable
          dataAvailable
          dataAssigned
          dataPercentage
        }
      }
    }
  }
}
"""


class Mobiel50PlusAuthError(Exception):
    """Raised when login fails."""


class Mobiel50PlusApiClient:
    """Talks to the 50+ Mobiel customer portal API."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._access_token: str | None = None

    async def async_login(self) -> None:
        """Authenticate and store a bearer token for subsequent requests.

        Mirrors the SPA's own flow: two `/verifyLogin` calls (email-only,
        then email+password) that validate credentials, followed by
        `/oauth/token` to actually issue the bearer token.
        """
        await self._async_verify_login(password=None, step=None)
        step = await self._async_verify_login(password=self._password, step="password")
        if step != "login":
            raise Mobiel50PlusAuthError(
                f"Unexpected login step '{step}' — this account may require "
                "an authentication factor (e.g. phone/2FA) this client doesn't support"
            )

        async with self._session.post(
            TOKEN_URL,
            json={
                "grant_type": "password",
                "client_id": OAUTH_CLIENT_ID,
                "scope": "CUSTOMER",
                "username": self._username,
                "password": self._password,
            },
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401:
                raise Mobiel50PlusAuthError("Login failed")
            resp.raise_for_status()
            data = await resp.json()

        access_token = data.get("access_token")
        if not access_token:
            raise Mobiel50PlusAuthError(
                data.get("error_description") or data.get("error") or "Login failed"
            )
        self._access_token = access_token

    async def _async_verify_login(self, *, password: str | None, step: str | None) -> str | None:
        """Run one step of the `/verifyLogin` handshake, return the resulting step."""
        async with self._session.post(
            VERIFY_LOGIN_URL,
            json={
                "username": self._username,
                "phoneNumber": None,
                "password": password,
                "step": step,
                "response": "",
            },
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if data.get("message"):
            raise Mobiel50PlusAuthError(data["message"])
        return data.get("step")

    async def async_get_status(self) -> dict:
        """Fetch current bundle/usage status for the logged-in account."""
        if self._access_token is None:
            await self.async_login()

        payload = await self._async_graphql_status()
        if payload is None:
            # Token expired/revoked — re-authenticate and retry once.
            await self.async_login()
            payload = await self._async_graphql_status()
            if payload is None:
                raise Mobiel50PlusAuthError("Status fetch failed after re-authenticating")

        subscription_group = payload["data"]["me"]["subscriptionGroups"][0]
        balance = subscription_group["msisdns"][0]["balance"]

        days_to_bundle_refresh = subscription_group.get("remainingBeforeBill")
        bundle_refresh_date = (
            date.today() + timedelta(days=days_to_bundle_refresh)
            if days_to_bundle_refresh is not None
            else None
        )

        active_contract = subscription_group.get("activeContract")
        contract_end_date = (
            date.fromisoformat(active_contract["endDate"])
            if active_contract and active_contract.get("endDate")
            else None
        )

        return {
            "remaining_mb": balance["dataAvailable"],
            "bundle_size_mb": balance["dataAssigned"],
            "remaining_minutes": balance["voiceAvailable"],
            "remaining_sms": balance["smsAvailable"],
            "data_percentage": balance["dataPercentage"],
            "days_to_bundle_refresh": days_to_bundle_refresh,
            "bundle_refresh_date": bundle_refresh_date,
            "contract_end_date": contract_end_date,
        }

    async def _async_graphql_status(self) -> dict | None:
        """Run the status GraphQL query. Returns None on a 401 (bad/expired token)."""
        async with self._session.post(
            GRAPHQL_URL,
            headers={"Authorization": f"Bearer {self._access_token}"},
            json={
                "operationName": "getCustomerForMsisdn",
                "variables": {},
                "query": STATUS_QUERY,
            },
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401:
                return None
            resp.raise_for_status()
            return await resp.json()
