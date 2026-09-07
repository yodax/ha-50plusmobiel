"""Tests for the mobiel50plus config flow, including the reauth flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobiel50plus.api import Mobiel50PlusAuthError
from custom_components.mobiel50plus.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_ACCOUNT_NAME,
    DOMAIN,
)

USERNAME = "user@example.com"
PASSWORD = "hunter2"

LOGIN_PATCH_TARGET = "custom_components.mobiel50plus.api.Mobiel50PlusApiClient.async_login"
NAME_PATCH_TARGET = (
    "custom_components.mobiel50plus.api.Mobiel50PlusApiClient"
    ".async_get_account_first_name"
)


async def test_user_step_success_creates_entry(hass: HomeAssistant) -> None:
    with (
        patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)),
        patch(NAME_PATCH_TARGET, new=AsyncMock(return_value="Sam")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The title is a display name, never the username — see the entry-title
    # tests at the bottom of this file.
    assert result["title"] == "Sam"
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_step_duplicate_username_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    ).add_to_hass(hass)

    with patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_step_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    with patch(
        LOGIN_PATCH_TARGET, new=AsyncMock(side_effect=Mobiel50PlusAuthError("bad creds"))
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_unexpected_error_shows_unknown(hass: HomeAssistant) -> None:
    with patch(LOGIN_PATCH_TARGET, new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_reauth_flow_success_updates_password_and_reloads(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old-password"},
    )
    entry.add_to_hass(hass)

    with (
        patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)),
        patch(NAME_PATCH_TARGET, new=AsyncMock(return_value="Sam")),
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_USERNAME] == USERNAME


async def test_reauth_flow_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old-password"},
    )
    entry.add_to_hass(hass)

    with patch(
        LOGIN_PATCH_TARGET, new=AsyncMock(side_effect=Mobiel50PlusAuthError("still wrong"))
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "still-wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    # The password isn't updated on a failed reauth attempt.
    assert entry.data[CONF_PASSWORD] == "old-password"


# ── Entry titles must not carry the account's email address ──────────────────
#
# FAILING-FIRST: every test below failed against `title=user_input[CONF_USERNAME]`.
# `test_user_step_title_does_not_contain_the_email_address` in particular failed
# with `assert "user@example.com" not in "user@example.com"`, which is the exact
# leak it exists to prevent: HA slugifies the entry title into the device name
# and from there into every entity_id and friendly_name, so the address ends up
# in `sensor.<address>_data_bundle_remaining` and in any dashboard YAML pasted
# into a forum thread or an issue on this repo.

async def _run_user_step(
    hass: HomeAssistant, username: str, first_name: str | None
) -> dict:
    with (
        patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)),
        patch(NAME_PATCH_TARGET, new=AsyncMock(return_value=first_name)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: username, CONF_PASSWORD: PASSWORD}
        )


async def test_user_step_title_does_not_contain_the_email_address(
    hass: HomeAssistant,
) -> None:
    """The regression guard. A future `title=username` "simplification" fails here."""
    result = await _run_user_step(hass, USERNAME, "Sam")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert USERNAME not in result["title"]
    assert "@" not in result["title"]
    # The domain is the half that identifies an employer or provider.
    assert "example.com" not in result["title"]
    # ...while the credentials themselves are still stored on the entry.
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_step_titles_from_the_api_first_name(hass: HomeAssistant) -> None:
    result = await _run_user_step(hass, USERNAME, "Sam")

    assert result["title"] == "Sam"


async def test_user_step_falls_back_to_the_email_local_part(
    hass: HomeAssistant,
) -> None:
    """No first name from the API — the local part, never the domain."""
    result = await _run_user_step(hass, "dean@kroes.example", None)

    assert result["title"] == "dean"


async def test_user_step_falls_back_to_a_constant_without_a_local_part(
    hass: HomeAssistant,
) -> None:
    result = await _run_user_step(hass, "@no-local-part.example", None)

    assert result["title"] == DEFAULT_ACCOUNT_NAME


async def test_user_step_never_titles_an_entry_with_an_address_shaped_first_name(
    hass: HomeAssistant,
) -> None:
    """Defence in depth: an address arriving in `firstName` is still not a title."""
    result = await _run_user_step(hass, "dean@kroes.example", "dean@kroes.example")

    assert "@" not in result["title"]
    assert result["title"] == "dean"


async def test_user_step_title_survives_a_failed_name_lookup(
    hass: HomeAssistant,
) -> None:
    """A naming nicety must never block adding the integration."""
    with (
        patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)),
        patch(NAME_PATCH_TARGET, new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "sam@kroes.example", CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "sam"


async def test_second_household_account_gets_a_distinct_title(
    hass: HomeAssistant,
) -> None:
    """`firstName` is the *contract holder's* name, identical across a household's
    accounts — confirmed live against four real accounts on one contract, which
    all report the same first name under four different logins. Titling them all
    the same would be worse than the address it replaces, so the local part
    disambiguates.
    """
    MockConfigEntry(
        domain=DOMAIN,
        title="Michael",
        unique_id="michael@example.invalid",
        data={CONF_USERNAME: "michael@example.invalid", CONF_PASSWORD: PASSWORD},
    ).add_to_hass(hass)

    result = await _run_user_step(hass, "dean@kroes.example", "Michael")

    assert result["title"] == "Michael (dean)"
    assert "@" not in result["title"]


async def test_title_collision_is_matched_case_insensitively(
    hass: HomeAssistant,
) -> None:
    """"michael" and "Michael" slugify to the same entity_id prefix."""
    MockConfigEntry(
        domain=DOMAIN,
        title="michael",
        unique_id="michael@example.invalid",
        data={CONF_USERNAME: "michael@example.invalid", CONF_PASSWORD: PASSWORD},
    ).add_to_hass(hass)

    result = await _run_user_step(hass, "dean@kroes.example", "Michael")

    assert result["title"] == "Michael (dean)"


async def test_reauth_does_not_rename_an_existing_entry(hass: HomeAssistant) -> None:
    """Existing installs keep their titles — and therefore their entity IDs.

    Renaming on reauth would break dashboards and automations that reference
    the current entity IDs; migrating them is a separate, deliberate decision.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old-password"},
    )
    entry.add_to_hass(hass)

    with (
        patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)),
        patch(NAME_PATCH_TARGET, new=AsyncMock(return_value="Michael")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.title == USERNAME
