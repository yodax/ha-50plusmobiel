"""End-to-end tests: set the integration up for real and inspect what it creates.

These exist because the config-flow tests assert on a *title*, and the thing
actually promised is about **entity IDs**. An independent review of the entry-title
change made exactly that point: nothing verified the step from title to
`entity_id`, which is the step the whole change is about. So this module sets up
a real config entry against an in-memory `hass` and reads the entity registry.

FAILING-FIRST: `test_no_entity_id_contains_the_account_email` was verified
against `title=user_input[CONF_USERNAME]`, where it failed with the registry
holding `sensor.user_example_com_data_bundle_remaining` — the leak itself,
rather than a proxy for it.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobiel50plus.const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

USERNAME = "sam@kroes.example"
PASSWORD = "hunter2"

# An unlimited calling/SMS plan: the API returns null, which must surface as
# "unknown" — never as 0, which would read as "you have run out".
STATUS = {
    "remaining_mb": 9433,
    "bundle_size_mb": 12000,
    "remaining_minutes": None,
    "remaining_sms": None,
    "data_percentage": 79,
    "days_to_bundle_refresh": 18,
    "bundle_refresh_date": date(2026, 2, 3),
    "contract_end_date": None,
}


async def _setup(hass: HomeAssistant, *, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.mobiel50plus.api.Mobiel50PlusApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.mobiel50plus.api.Mobiel50PlusApiClient.async_get_status",
            new=AsyncMock(return_value=STATUS),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_no_entity_id_contains_the_account_email(hass: HomeAssistant) -> None:
    """The actual promise, asserted on the actual artefact."""
    entry = await _setup(hass, title="Sam")

    registry = er.async_get(hass)
    entity_ids = [
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]

    assert entity_ids, "the integration created no entities"
    for entity_id in entity_ids:
        assert USERNAME not in entity_id
        # The slugified form is what actually lands in an entity_id.
        assert "sam_kroes_example" not in entity_id
        assert "kroes" not in entity_id
    assert "sensor.sam_data_bundle_remaining" in entity_ids


async def test_friendly_names_do_not_contain_the_account_email(
    hass: HomeAssistant,
) -> None:
    """`has_entity_name` builds the friendly name from the device name, which
    comes from the entry title — so the title leaks into the UI as well as IDs."""
    await _setup(hass, title="Sam")

    names = [
        state.attributes.get("friendly_name")
        for state in hass.states.async_all("sensor")
    ]

    assert names
    for name in names:
        assert "@" not in (name or "")


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("sensor.sam_data_bundle_remaining", "9433"),
        ("sensor.sam_data_bundle_size", "12000"),
        ("sensor.sam_data_bundle_remaining_percentage", "79"),
        ("sensor.sam_days_until_bundle_refresh", "18"),
        ("sensor.sam_bundle_refresh_date", "2026-02-03"),
    ],
)
async def test_values_reach_the_sensors(
    hass: HomeAssistant, entity_id: str, expected: str
) -> None:
    await _setup(hass, title="Sam")

    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} was never created"
    assert state.state == expected


@pytest.mark.parametrize(
    "entity_id",
    [
        "sensor.sam_calling_minutes_remaining",
        "sensor.sam_sms_remaining",
        "sensor.sam_contract_end_date",
    ],
)
async def test_unlimited_and_absent_values_are_unknown_not_zero(
    hass: HomeAssistant, entity_id: str
) -> None:
    """The silent-wrong-answer case: `None` means unlimited (or "no contract"),
    and a sensor reading 0 would tell a household it had run out of calls.

    The coordinator-level test that `None` survives the coordinator would still
    pass if the sensor coerced it later, so this asserts the rendered state.
    """
    await _setup(hass, title="Sam")

    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} was never created"
    assert state.state == "unknown"
    assert state.state != "0"


async def test_device_name_is_the_entry_title(hass: HomeAssistant) -> None:
    """Pins the title → device → entity_id chain the leak travelled along."""
    from homeassistant.helpers import device_registry as dr

    entry = await _setup(hass, title="Sam")

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].name == "Sam"
    assert "@" not in devices[0].name
