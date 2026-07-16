"""Client for the (unofficial) mijn.50plusmobiel.nl API.

mijn.50plusmobiel.nl is a client-rendered SPA backed by a GraphQL API. The
SPA's login form drives a separate `verifyLogin` two-step (email, then
password) endpoint with a reCAPTCHA response slot, but that flow turns out
to be purely a UI affordance: the actual `/token/login` endpoint it ends
with is stateless and works standalone with just username+password, no
cookies or prior verifyLogin call required. So this client skips the
verifyLogin dance entirely.

Endpoints (captured via devtools against a real account, see CLAUDE.md):

- ``POST /token/login`` — body ``{"username": ..., "password": ...}``.
  Always returns HTTP 200. Success is signalled by an ``access_token``
  field (a JWT, valid ~2 years per ``expires_in``); failure by an
  ``{"error": "invalid_grant", ...}`` body with no token — the status code
  does *not* distinguish these.
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
LOGIN_URL = f"{API_BASE}/token/login"
GRAPHQL_URL = f"{API_BASE}/api/graphql"

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
        """Authenticate and store a bearer token for subsequent requests."""
        async with self._session.post(
            LOGIN_URL,
            json={"username": self._username, "password": self._password},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        access_token = data.get("access_token")
        if not access_token:
            raise Mobiel50PlusAuthError(
                data.get("error_description") or data.get("error") or "Login failed"
            )
        self._access_token = access_token

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
