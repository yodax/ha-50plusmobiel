"""Tests for the per-install polling offset.

FAILING-FIRST: every test here failed with ImportError before `poll_jitter`
and `MAX_POLL_JITTER` existed, and `test_offset_is_stable_across_processes`
was additionally verified against a deliberate `hash()`-based implementation
— it is the only test in this file that catches that mistake.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from custom_components.mobiel50plus.const import DEFAULT_SCAN_INTERVAL, MAX_POLL_JITTER
from custom_components.mobiel50plus.coordinator import poll_jitter


class TestPollJitter:
    def test_offset_is_within_the_window(self) -> None:
        for n in range(500):
            offset = poll_jitter(f"01M1XERWVWMQKHFAT35E7VWCJ{n}")
            assert timedelta() <= offset < MAX_POLL_JITTER

    def test_offset_is_whole_seconds(self) -> None:
        for n in range(50):
            assert poll_jitter(f"entry-{n}").microseconds == 0

    def test_same_seed_gives_the_same_offset(self) -> None:
        assert poll_jitter("01M1XERWVWMQKHFAT35E7VWCJY") == poll_jitter(
            "01M1XERWVWMQKHFAT35E7VWCJY"
        )

    def test_offset_is_stable_across_processes(self) -> None:
        """The trap this guards: Python randomises `hash()` per process.

        Using the builtin `hash()` here would look deterministic and would in
        fact re-roll the offset on every Home Assistant restart. Recomputing in
        a subprocess with a different PYTHONHASHSEED is the only way to catch
        that, since within one process the builtin looks perfectly stable.
        """
        seeds = ["entry-a", "entry-b", "01M1XERWVWMQKHFAT35E7VWCJY"]
        expected = [poll_jitter(seed).total_seconds() for seed in seeds]

        script = (
            "import sys;"
            "sys.path.insert(0, %r);"
            "from custom_components.mobiel50plus.coordinator import poll_jitter;"
            "print([poll_jitter(s).total_seconds() for s in %r])"
            % (str(Path(__file__).parent.parent), seeds)
        )
        env = {**os.environ, "PYTHONHASHSEED": "1"}
        first = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )
        env["PYTHONHASHSEED"] = "12345"
        second = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert eval(first.stdout) == expected
        assert eval(second.stdout) == expected

    def test_different_entries_get_different_offsets(self) -> None:
        """The whole point — two installs must not land on the same second."""
        offsets = {poll_jitter(f"entry-{n}") for n in range(200)}

        # 15 minutes of whole seconds is 900 buckets; 200 draws should fill a
        # large fraction of them rather than clustering.
        assert len(offsets) > 150

    def test_offsets_spread_across_the_whole_window(self) -> None:
        offsets = [poll_jitter(f"entry-{n}").total_seconds() for n in range(500)]
        window = MAX_POLL_JITTER.total_seconds()

        # Each quarter of the window should get a meaningful share; a bug that
        # collapsed the fraction (e.g. dividing by the wrong power of two)
        # would pile every install into one corner.
        for quarter in range(4):
            low, high = window * quarter / 4, window * (quarter + 1) / 4
            assert sum(1 for o in offsets if low <= o < high) > 50

    def test_seed_is_not_required_to_look_like_a_ulid(self) -> None:
        """Any string works — the seed is opaque to this function."""
        assert timedelta() <= poll_jitter("") < MAX_POLL_JITTER
        assert timedelta() <= poll_jitter("hé — unicode 🙂") < MAX_POLL_JITTER

    def test_jitter_window_is_small_next_to_the_poll_interval(self) -> None:
        """A phase shift, not a second polling schedule."""
        assert MAX_POLL_JITTER < DEFAULT_SCAN_INTERVAL / 2
