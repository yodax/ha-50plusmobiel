"""Tests for the mobiel50plus config flow, including the reauth flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobiel50plus.api import Mobiel50PlusAuthError
from custom_components.mobiel50plus.const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

USERNAME = "user@example.com"
PASSWORD = "hunter2"

LOGIN_PATCH_TARGET = "custom_components.mobiel50plus.api.Mobiel50PlusApiClient.async_login"


async def test_user_step_success_creates_entry(hass: HomeAssistant) -> None:
    with patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
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

    with patch(LOGIN_PATCH_TARGET, new=AsyncMock(return_value=None)):
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
