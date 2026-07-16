"""Sensor platform for the 50+ Mobiel integration."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Mobiel50PlusCoordinator

# Keys line up with the dict returned by api.py's async_get_status(). Calls
# and SMS are unlimited on this account's plan, so their "available" fields
# come back as None from the API — sensors show unknown/unavailable rather
# than a number in that case, which is expected, not a bug. Same applies to
# bundle_refresh_date/contract_end_date if the account has no active contract.
SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="remaining_mb",
        translation_key="remaining_mb",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal-4g",
    ),
    SensorEntityDescription(
        key="bundle_size_mb",
        translation_key="bundle_size_mb",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:database",
    ),
    SensorEntityDescription(
        key="data_percentage",
        translation_key="data_percentage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent-circle",
    ),
    SensorEntityDescription(
        key="remaining_minutes",
        translation_key="remaining_minutes",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:phone",
    ),
    SensorEntityDescription(
        key="remaining_sms",
        translation_key="remaining_sms",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-text",
    ),
    SensorEntityDescription(
        key="days_to_bundle_refresh",
        translation_key="days_to_bundle_refresh",
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-refresh",
    ),
    SensorEntityDescription(
        key="bundle_refresh_date",
        translation_key="bundle_refresh_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-sync",
    ),
    SensorEntityDescription(
        key="contract_end_date",
        translation_key="contract_end_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-end",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up 50+ Mobiel sensors for one account (config entry)."""
    coordinator: Mobiel50PlusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        Mobiel50PlusSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class Mobiel50PlusSensor(CoordinatorEntity[Mobiel50PlusCoordinator], SensorEntity):
    """A single bundle/usage value for one 50+ Mobiel account."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Mobiel50PlusCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="50+ Mobiel",
            configuration_url="https://mijn.50plusmobiel.nl",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)
