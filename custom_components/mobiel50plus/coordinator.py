"""DataUpdateCoordinator for the 50+ Mobiel integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Mobiel50PlusApiClient, Mobiel50PlusApiError, Mobiel50PlusAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_POLL_JITTER

_LOGGER = logging.getLogger(__name__)


def poll_jitter(seed: str) -> timedelta:
    """This install's fixed phase shift, in [0, MAX_POLL_JITTER).

    50+ Mobiel is a small MVNO and this is a published integration, so every
    copy hitting the portal in lockstep is worth avoiding. This coordinator
    free-runs on a plain `update_interval` anchored to Home Assistant startup,
    so installs are already de-phased by whenever each instance happened to
    boot — the jitter is a second, deliberate axis of spread rather than the
    only one.

    **`hashlib`, not the builtin `hash()`.** Python randomises string hashing
    per process, so `hash()` here would silently produce a different offset
    after every Home Assistant restart — turning a deliberately stable offset
    back into a per-restart random one while the code still read as
    deterministic. `test_offset_is_stable_across_processes` pins this; no
    in-process test can, because within one process `hash()` looks perfectly
    stable.

    Seeded from the config entry id, not the account email. `entry_id` is a
    random per-entry ULID, so it spreads a multi-account household across
    several offsets *and* gives two Home Assistant instances tracking the same
    account different offsets — which `unique_id` (the email) would not. It
    also keeps an email address out of the calculation entirely.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return timedelta(seconds=int(MAX_POLL_JITTER.total_seconds() * fraction))


class Mobiel50PlusCoordinator(DataUpdateCoordinator[dict]):
    """Polls the 50+ Mobiel API for one account's bundle/usage status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: Mobiel50PlusApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        # Applied once, to the first scheduled poll only — see _schedule_refresh.
        self._pending_jitter: timedelta | None = poll_jitter(entry.entry_id)
        _LOGGER.debug(
            "Polling every %s, first poll delayed a further %s for this install",
            DEFAULT_SCAN_INTERVAL,
            self._pending_jitter,
        )

    @callback
    def _schedule_refresh(self) -> None:
        """Phase-shift the first scheduled poll, then behave exactly as normal.

        Shifting once is enough: every later poll inherits the phase from this
        one. Done by lending the base class a longer interval for the single
        call rather than reimplementing its scheduling, which also handles
        `pref_disable_polling` and request-retry backoff.

        The offset is *consumed only if it was actually applied*. An earlier
        version cleared it up-front, so on an entry with polling disabled, or on
        a call the base class answered with its retry-after backoff instead, the
        offset was silently spent on a poll that never got shifted — the install
        then ran forever with no spread at all. `_unsub_refresh` being set is
        the base class's own evidence that it scheduled something.

        Not covered: a manual refresh before the first timer fires cancels that
        timer and reschedules without the offset. That is not worth engineering
        around — a manual refresh re-anchors the whole schedule to an arbitrary
        moment anyway, which spreads it just as well.

        `_schedule_refresh`, `_retry_after` and `_unsub_refresh` are Home
        Assistant private API. They are read through `getattr` so that an HA
        version which renames or drops one degrades to "no offset" rather than
        raising — polling matters, spreading it is a nicety.
        """
        jitter = self._pending_jitter
        entry = self.config_entry
        if (
            jitter is None
            or self.update_interval is None
            or getattr(self, "_retry_after", None) is not None
            or (entry is not None and entry.pref_disable_polling)
        ):
            super()._schedule_refresh()
            return

        base_interval = self.update_interval
        self.update_interval = base_interval + jitter
        try:
            super()._schedule_refresh()
        finally:
            self.update_interval = base_interval

        if getattr(self, "_unsub_refresh", None) is not None:
            self._pending_jitter = None

    async def _async_update_data(self) -> dict:
        try:
            return await self.client.async_get_status()
        except Mobiel50PlusAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except Mobiel50PlusApiError as err:
            raise UpdateFailed(str(err)) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with 50+ Mobiel: {err}") from err
