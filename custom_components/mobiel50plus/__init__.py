"""The 50+ Mobiel integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Mobiel50PlusApiClient
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import Mobiel50PlusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 50+ Mobiel from a config entry (one per account)."""
    session = async_get_clientsession(hass)
    client = Mobiel50PlusApiClient(
        session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = Mobiel50PlusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
