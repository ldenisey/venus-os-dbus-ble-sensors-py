# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Unit tests for load_throttle.LoadThrottle.

The throttle's only inputs are ``/proc/loadavg`` and the wall clock.
We control the loadavg file via a tempfile and the callback side
effects via lambdas — no D-Bus, no real /proc, no GLib.
"""

from __future__ import annotations

import logging

import pytest

from load_throttle import (
    LoadThrottle,
    TRIP_5M,
    TRIP_15M,
    RELEASE_5M,
    RELEASE_15M,
)


def _write_loadavg(path, *, l1=1.0, l5=2.0, l15=3.0):
    """Write a ``/proc/loadavg``-shaped file at ``path``."""
    path.write_text("%.2f %.2f %.2f 1/100 12345\n" % (l1, l5, l15))


def _make(tmp_path, *, trip_calls=None, release_calls=None, **kwargs):
    """Construct a LoadThrottle pointed at a fake /proc/loadavg under
    ``tmp_path``.  Returns ``(throttle, loadavg_path)``."""
    loadavg = tmp_path / "loadavg"
    _write_loadavg(loadavg)  # initial: safe values

    if trip_calls is None:
        trip_calls = []
    if release_calls is None:
        release_calls = []

    t = LoadThrottle(
        on_trip=lambda l5, l15: trip_calls.append((l5, l15)),
        on_release=lambda l5, l15: release_calls.append((l5, l15)),
        loadavg_path=str(loadavg),
        **kwargs,
    )
    return t, loadavg


# ── trip thresholds ────────────────────────────────────────────────────────


class TestTrip:
    def test_below_both_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=4.0, l15=4.0)
        t.tick()
        assert not t.is_throttled
        assert trips == []

    def test_15m_at_threshold_trips(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=TRIP_15M)
        t.tick()
        assert t.is_throttled
        assert trips == [(2.0, TRIP_15M)]

    def test_15m_above_threshold_trips(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=TRIP_15M + 0.1)
        t.tick()
        assert t.is_throttled
        assert trips and trips[0][1] == pytest.approx(TRIP_15M + 0.1)

    def test_5m_at_threshold_trips_even_if_15m_safe(self, tmp_path):
        """The OR condition fires on the 5-min signal alone."""
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=TRIP_5M, l15=2.0)
        t.tick()
        assert t.is_throttled

    def test_15m_just_below_5_5_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=5.49)
        t.tick()
        assert not t.is_throttled
        assert trips == []

    def test_5m_just_below_6_0_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=5.99, l15=2.0)
        t.tick()
        assert not t.is_throttled

    def test_trip_callback_fires_only_once(self, tmp_path):
        """Successive ticks while throttled should not re-fire on_trip."""
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        t.tick()
        t.tick()
        assert len(trips) == 1


# ── release thresholds ─────────────────────────────────────────────────────


class TestRelease:
    def test_release_requires_both_below(self, tmp_path):
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        assert t.is_throttled

        # 15-min dropped but 5-min still high → still throttled
        _write_loadavg(lf, l5=5.5, l15=4.0)
        t.tick()
        assert t.is_throttled
        assert releases == []

        # 5-min dropped but 15-min still high → still throttled
        _write_loadavg(lf, l5=2.0, l15=5.5)
        t.tick()
        assert t.is_throttled
        assert releases == []

        # Both below → release
        _write_loadavg(lf, l5=4.99, l15=4.99)
        t.tick()
        assert not t.is_throttled
        assert releases == [(4.99, 4.99)]

    def test_release_at_threshold_does_not_release(self, tmp_path):
        """Release requires strictly less-than, not equal."""
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()

        _write_loadavg(lf, l5=RELEASE_5M, l15=RELEASE_15M)
        t.tick()
        assert t.is_throttled

    def test_release_callback_fires_only_once(self, tmp_path):
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        _write_loadavg(lf, l5=1.0, l15=1.0)
        t.tick()
        t.tick()
        t.tick()
        assert len(releases) == 1


# ── re-trip after release ──────────────────────────────────────────────────


class TestReTrip:
    def test_can_trip_again_after_release(self, tmp_path):
        trips: list = []
        releases: list = []
        t, lf = _make(tmp_path, trip_calls=trips, release_calls=releases)

        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        _write_loadavg(lf, l5=1.0, l15=1.0)
        t.tick()
        _write_loadavg(lf, l5=8.0, l15=8.0)
        t.tick()

        assert t.is_throttled
        assert len(trips) == 2
        assert len(releases) == 1


# ── /proc/loadavg parsing edge cases ───────────────────────────────────────


class TestLoadavgParsing:
    def test_returns_true_even_when_loadavg_unreadable(self, tmp_path, caplog):
        """tick() must keep returning True so the GLib timeout keeps firing."""
        t, _lf = _make(tmp_path)
        t._loadavg_path = str(tmp_path / "does-not-exist")
        with caplog.at_level(logging.WARNING, logger="load_throttle"):
            assert t.tick() is True
        assert any("failed to read" in r.message for r in caplog.records)

    def test_malformed_loadavg_is_handled(self, tmp_path):
        t, lf = _make(tmp_path)
        lf.write_text("garbage no spaces")
        # Should not raise, should not change state
        assert t.tick() is True
        assert not t.is_throttled

    def test_last_load_values_exposed(self, tmp_path):
        t, lf = _make(tmp_path)
        _write_loadavg(lf, l5=3.14, l15=2.71)
        t.tick()
        assert t.last_load_5m == pytest.approx(3.14)
        assert t.last_load_15m == pytest.approx(2.71)


# ── callback errors don't crash tick() ─────────────────────────────────────


class TestCallbackResilience:
    def test_on_trip_raising_does_not_crash_tick(self, tmp_path, caplog):
        def bad(_l5, _l15):
            raise RuntimeError("intentional")

        t = LoadThrottle(
            on_trip=bad,
            loadavg_path=str(tmp_path / "loadavg"),
        )
        _write_loadavg(tmp_path / "loadavg", l5=10.0, l15=10.0)
        with caplog.at_level(logging.ERROR, logger="load_throttle"):
            assert t.tick() is True
        # state still updates (we are throttled) even though callback raised
        assert t.is_throttled
        assert any("on_trip callback raised" in r.message for r in caplog.records)

    def test_on_release_raising_does_not_crash_tick(self, tmp_path, caplog):
        def bad(_l5, _l15):
            raise RuntimeError("intentional")

        loadavg = tmp_path / "loadavg"
        _write_loadavg(loadavg, l5=10.0, l15=10.0)
        t = LoadThrottle(
            on_release=bad,
            loadavg_path=str(loadavg),
        )
        t.tick()
        _write_loadavg(loadavg, l5=1.0, l15=1.0)
        with caplog.at_level(logging.ERROR, logger="load_throttle"):
            t.tick()
        assert not t.is_throttled
