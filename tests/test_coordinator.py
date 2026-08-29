"""Tests for Mobiel50PlusCoordinator's exception-to-HA-behaviour mapping."""
from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Importing config_flow registers Mobiel50PlusConfigFlow in HA's HANDLERS
# registry — required for async_start_reauth_if_available() to actually do
# anything (it silently no-ops for domains with no known config flow).
from custom_components.mobiel50plus import config_flow  # noqa: F401
from custom_components.mobiel50plus.api import (
    Mobiel50PlusApiClient,
    Mobiel50PlusApiError,
    Mobiel50PlusAuthError,
)
from custom_components.mobiel50plus.const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from custom_components.mobiel50plus.coordinator import Mobiel50PlusCoordinator


def _setup_coordinator(
    hass: HomeAssistant, *, side_effect: Exception | None = None, result: dict | None = None
) -> tuple[Mobiel50PlusCoordinator, MockConfigEntry]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user@example.com",
        data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "hunter2"},
    )
    entry.add_to_hass(hass)

    client = AsyncMock(spec=Mobiel50PlusApiClient)
    if side_effect is not None:
        client.async_get_status.side_effect = side_effect
    else:
        client.async_get_status.return_value = result

    coordinator = Mobiel50PlusCoordinator(hass, entry, client)
    return coordinator, entry


def _active_reauth_flows(hass: HomeAssistant) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == "reauth"
    ]


async def test_successful_update_stores_data(hass: HomeAssistant) -> None:
    data = {"remaining_mb": 9433}
    coordinator, _entry = _setup_coordinator(hass, result=data)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == data
    assert not _active_reauth_flows(hass)


async def test_auth_error_marks_failed_and_starts_reauth(hass: HomeAssistant) -> None:
    coordinator, entry = _setup_coordinator(
        hass, side_effect=Mobiel50PlusAuthError("bad password")
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    reauth_flows = _active_reauth_flows(hass)
    assert len(reauth_flows) == 1
    assert reauth_flows[0]["context"]["entry_id"] == entry.entry_id


async def test_api_error_marks_failed_without_reauth(hass: HomeAssistant) -> None:
    """Malformed/unexpected API responses aren't a credentials problem."""
    coordinator, _entry = _setup_coordinator(
        hass, side_effect=Mobiel50PlusApiError("unexpected shape")
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert not _active_reauth_flows(hass)


@pytest.mark.parametrize(
    "exc",
    [aiohttp.ClientError("boom"), TimeoutError("timed out")],
)
async def test_transport_error_marks_failed_without_reauth(
    hass: HomeAssistant, exc: Exception
) -> None:
    coordinator, _entry = _setup_coordinator(hass, side_effect=exc)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert not _active_reauth_flows(hass)
