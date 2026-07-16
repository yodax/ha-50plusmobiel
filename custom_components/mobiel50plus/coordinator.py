"""DataUpdateCoordinator for the 50+ Mobiel integration."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Mobiel50PlusApiClient, Mobiel50PlusAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class Mobiel50PlusCoordinator(DataUpdateCoordinator[dict]):
    """Polls the 50+ Mobiel API for one account's bundle/usage status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: Mobiel50PlusApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict:
        try:
            return await self.client.async_get_status()
        except Mobiel50PlusAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with 50+ Mobiel: {err}") from err
