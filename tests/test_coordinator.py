"""Tests for Mobiel50PlusCoordinator's exception-to-HA-behaviour mapping."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
from custom_components.mobiel50plus.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.mobiel50plus.coordinator import (
    Mobiel50PlusCoordinator,
    poll_jitter,
)


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


class TestFirstPollIsPhaseShifted:
    """The jitter must actually reach the scheduler, not just exist as a function.

    FAILING-FIRST: `test_first_refresh_is_delayed_by_the_jitter` failed against
    the pre-fix coordinator with `assert 3600.0 == 3600.0 + jitter` — the poll
    was scheduled at exactly the bare interval, so every install polled in
    lockstep relative to its own startup.
    """

    def _scheduled_delay(self, hass: HomeAssistant, coordinator) -> float:
        """Seconds from now until the refresh the coordinator just scheduled."""
        with patch.object(hass.loop, "call_at", autospec=True) as call_at:
            call_at.return_value = MagicMock()
            coordinator._schedule_refresh()
        assert call_at.call_count == 1
        return call_at.call_args[0][0] - hass.loop.time()

    async def test_first_refresh_is_delayed_by_the_jitter(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, entry = _setup_coordinator(hass, result={})
        expected = poll_jitter(entry.entry_id)

        delay = self._scheduled_delay(hass, coordinator)

        assert delay == pytest.approx(
            DEFAULT_SCAN_INTERVAL.total_seconds() + expected.total_seconds(), abs=1.5
        )

    async def test_later_refreshes_use_the_bare_interval(
        self, hass: HomeAssistant
    ) -> None:
        """One phase shift, not a growing schedule — every later poll inherits it."""
        coordinator, _entry = _setup_coordinator(hass, result={})

        self._scheduled_delay(hass, coordinator)  # consumes the jitter
        second = self._scheduled_delay(hass, coordinator)
        third = self._scheduled_delay(hass, coordinator)

        assert second == pytest.approx(DEFAULT_SCAN_INTERVAL.total_seconds(), abs=1.5)
        assert third == pytest.approx(DEFAULT_SCAN_INTERVAL.total_seconds(), abs=1.5)

    async def test_update_interval_is_restored_after_the_shift(
        self, hass: HomeAssistant
    ) -> None:
        """The public attribute must not be left holding the shifted value."""
        coordinator, _entry = _setup_coordinator(hass, result={})

        self._scheduled_delay(hass, coordinator)

        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

    async def test_the_shift_is_seeded_from_entry_id_not_unique_id(
        self, hass: HomeAssistant
    ) -> None:
        """Two entries for the *same account* must still get different offsets.

        Asserted against fixed ids with known-distinct offsets rather than two
        random entry_ids: there are only 900 possible offsets, so a
        "two random ids differ" test collides roughly once in 900 runs and would
        fail for a reason that has nothing to do with the code.
        """
        seed_a, seed_b = "01M1XERWVWMQKHFAT35E7VWCJY", "01M1XERWVWMQKHFAT35E7VWCJZ"
        assert poll_jitter(seed_a) != poll_jitter(seed_b), "fixture seeds must differ"

        shared_unique_id = "user@example.com"
        coordinators = []
        for entry_id in (seed_a, seed_b):
            entry = MockConfigEntry(
                domain=DOMAIN,
                entry_id=entry_id,
                title="Sam",
                unique_id=shared_unique_id,
                data={CONF_USERNAME: shared_unique_id, CONF_PASSWORD: "hunter2"},
            )
            entry.add_to_hass(hass)
            coordinators.append(
                Mobiel50PlusCoordinator(hass, entry, AsyncMock(spec=Mobiel50PlusApiClient))
            )

        assert coordinators[0]._pending_jitter == poll_jitter(seed_a)
        assert coordinators[1]._pending_jitter == poll_jitter(seed_b)
        assert coordinators[0]._pending_jitter != coordinators[1]._pending_jitter

    async def test_offset_is_not_spent_when_polling_is_disabled(
        self, hass: HomeAssistant
    ) -> None:
        """Nothing was scheduled, so nothing was shifted — keep the offset.

        FAILING-FIRST: an earlier version cleared `_pending_jitter` up front, so
        an entry with polling disabled burned its offset on a poll that never
        happened and ran forever with no spread once polling was re-enabled.
        """
        coordinator, entry = _setup_coordinator(hass, result={})
        hass.config_entries.async_update_entry(entry, pref_disable_polling=True)

        coordinator._schedule_refresh()

        assert coordinator._pending_jitter == poll_jitter(entry.entry_id)

    async def test_offset_is_not_spent_on_a_retry_after_reschedule(
        self, hass: HomeAssistant
    ) -> None:
        """Retry-after backoff replaces the interval, so the shift never lands."""
        coordinator, entry = _setup_coordinator(hass, result={})
        coordinator._retry_after = 30

        with patch.object(hass.loop, "call_at", autospec=True) as call_at:
            call_at.return_value = MagicMock()
            coordinator._schedule_refresh()
        delay = call_at.call_args[0][0] - hass.loop.time()

        assert delay == pytest.approx(30, abs=1.5)
        assert coordinator._pending_jitter == poll_jitter(entry.entry_id)

    async def test_the_interval_itself_is_one_hour(self) -> None:
        """Pinned outright: every other test here derives from the constant, so
        quietly restoring the old 30 minutes would satisfy all of them."""
        assert DEFAULT_SCAN_INTERVAL == timedelta(hours=1)

    async def test_jitter_does_not_delay_the_first_data_fetch(
        self, hass: HomeAssistant
    ) -> None:
        """Setup must not wait out the offset — only the *next* poll is shifted."""
        data = {"remaining_mb": 1}
        coordinator, _entry = _setup_coordinator(hass, result=data)

        await coordinator.async_refresh()

        assert coordinator.data == data


class TestUnlimitedPlanValuesSurviveTheCoordinator:
    """`None` means "unlimited", and must reach the sensor as None — not as an
    error, and not coerced to 0, which would read as "you have run out"."""

    async def test_none_values_are_preserved(self, hass: HomeAssistant) -> None:
        data = {
            "remaining_mb": 9433,
            "remaining_minutes": None,
            "remaining_sms": None,
            "contract_end_date": None,
        }
        coordinator, _entry = _setup_coordinator(hass, result=data)

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.data["remaining_minutes"] is None
        assert coordinator.data["remaining_sms"] is None
        assert coordinator.data["contract_end_date"] is None
        assert not _active_reauth_flows(hass)
