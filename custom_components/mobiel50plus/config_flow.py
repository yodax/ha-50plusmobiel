"""Config flow for the 50+ Mobiel integration."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Mobiel50PlusApiClient, Mobiel50PlusAuthError
from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_ACCOUNT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


def account_title(
    first_name: str | None, username: str, existing_titles: Iterable[str]
) -> str:
    """A config entry title that is never the account's email address.

    Home Assistant slugifies the entry title into the device name and from there
    into every entity_id and friendly_name, so `title=username` put a real email
    address into `sensor.<address>_data_bundle_remaining` — and into any
    dashboard YAML a user pastes into a forum thread or an issue on this repo.

    Preference order:

    1. The account holder's first name from the API. No address at all, and the
       nicest default for the common single-account install.
    2. The email's **local part** — never the full address. The domain is the
       half that identifies an employer or a provider.
    3. A constant, for an account with neither.

    A collision with an existing entry is disambiguated with the local part,
    because `firstName` turns out to be the *contract holder's* name: four real
    accounts on one contract, under four different logins, all report the same
    first name. Titling them all identically would be worse than the address it
    replaces. Nothing here can emit an "@" — an address arriving in `firstName`
    is rejected rather than trusted.
    """
    local_part = username.partition("@")[0].strip()

    candidates = [
        (first_name or "").strip(),
        local_part,
        DEFAULT_ACCOUNT_NAME,
    ]
    base = next((c for c in candidates if c and "@" not in c), DEFAULT_ACCOUNT_NAME)

    # Case-insensitively: "Michael" and "michael" slugify to the same prefix.
    taken = {title.casefold() for title in existing_titles}
    if base.casefold() not in taken:
        return base

    if local_part and "@" not in local_part and local_part.casefold() != base.casefold():
        disambiguated = f"{base} ({local_part})"
        if disambiguated.casefold() not in taken:
            return disambiguated

    # Two accounts whose first name *and* local part match. Rare, and Home
    # Assistant's entity registry already suffixes duplicate entity IDs.
    return base


class Mobiel50PlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 50+ Mobiel — one entry per account."""

    VERSION = 1

    async def _async_validate_login(
        self, username: str, password: str
    ) -> tuple[dict[str, str], str | None]:
        """Try logging in.

        Returns a config-flow `errors` dict (empty on success) and, on success,
        the account holder's first name if the API offers one.
        """
        session = async_get_clientsession(self.hass)
        client = Mobiel50PlusApiClient(session, username, password)
        try:
            await client.async_login()
        except Mobiel50PlusAuthError:
            return {"base": "invalid_auth"}, None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating 50+ Mobiel login")
            return {"base": "unknown"}, None

        try:
            first_name = await client.async_get_account_first_name()
        except Exception:  # noqa: BLE001
            # The login worked; only the display name didn't. Never fail the
            # flow over a title — the caller falls back to the local part.
            _LOGGER.debug("Could not read a first name for the entry title", exc_info=True)
            first_name = None

        return {}, first_name

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            # unique_id stays the lowercased username: it is invisible in the UI,
            # and it is what stops the same account being added twice while
            # letting a household add several.
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()

            errors, first_name = await self._async_validate_login(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                return self.async_create_entry(
                    title=account_title(
                        first_name,
                        user_input[CONF_USERNAME],
                        [entry.title for entry in self._async_current_entries()],
                    ),
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication after a password change or lockout."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            errors, _first_name = await self._async_validate_login(
                reauth_entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                # Deliberately no `title=`. Retitling would not rewrite
                # existing entity IDs — those are fixed at creation — but it
                # *would* rename the device and, through `has_entity_name`,
                # every friendly name derived from it, so dashboards and
                # notifications would start showing a different label for
                # entities whose IDs had not moved. New naming applies to new
                # entries only; migrating an existing install is a separate,
                # deliberate decision.
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"username": reauth_entry.data[CONF_USERNAME]},
        )
