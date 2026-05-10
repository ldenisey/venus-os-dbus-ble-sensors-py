"""
Behavioural tests for ble_charger_common.ChargerCommonMixin.

Exercises every state-bearing method in the mixin without spinning up
a real device: GATT queue dedupe + drain, history accumulators,
charger-side alarm derivation, DVCC engagement transitions, and
settings persistence.

These cover commits 615fcd4 / f71d940 / c0ab2a7 and the full IP22
integration on top.
"""
from __future__ import annotations

import time
from typing import Optional

import pytest

import ble_charger_common as bcc

class _Subject(bcc.ChargerCommonMixin):
    """Minimal carrier class for the mixin's instance state.

    Real driver classes (BleDeviceIP22Charger, BleDeviceOrionTR)
    carry far more state — we only need what the mixin touches.
    """

    SETTINGS_NS_PREFIX = "test"

    def __init__(self, settings, writer, dev_mac="aa:bb:cc:dd:ee:ff",
                 passkey=14916):
        self._dbus_settings = settings
        self._pairing_passkey = passkey
        self._plog = "test:device"
        self.info = {"dev_mac": dev_mac}
        self._role_services = {}
        self._mode_busy = False
        self._writer = writer
        self._init_charger_common()

    @staticmethod
    def _gatt_writer():  # override the default singleton path
        # Late-bind to whatever ``writer`` was wired into the most
        # recently constructed instance via _set_writer.
        return _Subject._current_writer  # type: ignore[attr-defined]

@pytest.fixture
def subject(fake_settings, writer):
    _Subject._current_writer = writer  # type: ignore[attr-defined]
    return _Subject(settings=fake_settings, writer=writer)

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def test_persist_setting_creates_then_updates(subject, fake_settings):
    subject._persist_setting("ChargeCurrentLimit", 18.0)
    path = "/Settings/Devices/test_aabbccddeeff/ChargeCurrentLimit"
    assert path in fake_settings.values
    assert fake_settings.values[path] == 18.0
    assert path in fake_settings.created

    subject._persist_setting("ChargeCurrentLimit", 22.5)
    assert fake_settings.values[path] == 22.5
    # No second creation
    assert fake_settings.created.count(path) == 1

def test_try_get_setting_returns_none_when_missing(subject):
    assert subject._try_get_setting("Missing") is None

def test_try_get_setting_returns_persisted_value(subject, fake_settings):
    subject._persist_setting("AbsorptionVoltage", 14.4)
    assert subject._try_get_setting("AbsorptionVoltage") == 14.4

def test_load_persisted_seeds_role_paths(subject, fake_settings, fake_role):
    subject._persist_setting("ChargeCurrentLimit", 17.5)
    subject._persist_setting("AbsorptionVoltage", 14.2)
    subject._persist_setting("FloatVoltage",      13.6)

    subject.load_persisted_charger_settings(fake_role)

    assert fake_role.values["/Settings/ChargeCurrentLimit"] == 17.5
    assert fake_role.values["/Settings/AbsorptionVoltage"] == 14.2
    assert fake_role.values["/Settings/FloatVoltage"] == 13.6

def test_load_persisted_no_settings_is_no_op(subject, fake_role):
    subject.load_persisted_charger_settings(fake_role)
    # Nothing was set, nothing was raised.
    assert fake_role.values == {}

def test_load_persisted_seeds_history_state(subject, fake_settings):
    subject._persist_setting("History/OperationTime", 1234.5)
    subject._persist_setting("History/ChargedAh",     6.78)

    fresh = _Subject(settings=fake_settings,
                     writer=_Subject._current_writer)  # type: ignore
    assert fresh._history_op_time_s == 0.0
    assert fresh._history_charged_ah == 0.0

    fresh.load_persisted_charger_settings(rs := type("R", (), {})())
    rs.values = getattr(rs, "values", {})  # not used by the history path

    # The role-seed dict is empty because no /Settings/X paths existed,
    # but the in-memory accumulators were rehydrated from the History
    # sub-namespace.
    assert fresh._history_op_time_s == 1234.5
    assert fresh._history_charged_ah == 6.78

# ---------------------------------------------------------------------------
# History accumulators
# ---------------------------------------------------------------------------

def test_tick_history_first_call_primes_only(subject):
    # First tick has no `last`, so accumulators don't move.
    subject._tick_history(state=3, current_a=10.0)
    assert subject._history_op_time_s == 0.0
    assert subject._history_charged_ah == 0.0

def test_tick_history_charging_state_accumulates(subject):
    subject._tick_history(state=3, current_a=0.0)
    subject._history_last_tick = time.monotonic() - 5.0
    subject._tick_history(state=3, current_a=10.0)
    # 5 s of operation + 10 A * 5 s = 0.01389 Ah
    assert 4.5 <= subject._history_op_time_s <= 5.5
    assert pytest.approx(0.01389, rel=0.05) == subject._history_charged_ah

def test_tick_history_off_state_freezes_op_time(subject):
    subject._tick_history(state=3, current_a=10.0)
    subject._history_last_tick = time.monotonic() - 30.0
    subject._tick_history(state=4, current_a=5.0)
    op = subject._history_op_time_s

    subject._history_last_tick = time.monotonic() - 60.0
    subject._tick_history(state=0, current_a=0.0)
    # State=0 (off) does not tick OperationTime.
    assert subject._history_op_time_s == op

def test_tick_history_drops_unrealistic_gaps(subject):
    subject._tick_history(state=3, current_a=10.0)
    subject._history_last_tick = time.monotonic() - 3601.0  # 1h+ gap
    subject._tick_history(state=3, current_a=10.0)
    # 600 s threshold; 3600 s gap → discarded.
    assert subject._history_op_time_s == 0.0
    assert subject._history_charged_ah == 0.0

def test_tick_history_negative_current_does_not_subtract(subject):
    subject._tick_history(state=3, current_a=0.0)
    subject._history_last_tick = time.monotonic() - 10.0
    subject._tick_history(state=3, current_a=-5.0)  # impossible, but defensive
    # Operation time still ticks (state is in the active set).
    assert subject._history_op_time_s > 0
    # Negative current does NOT decrement ChargedAh.
    assert subject._history_charged_ah == 0.0

def test_publish_history_writes_op_time_always(subject, fake_role):
    subject._history_op_time_s = 3600.5
    subject._publish_history(fake_role)
    # OperationTime always ticks even on devices with no current data.
    assert fake_role.values["/History/Cumulative/User/OperationTime"] == 3600

def test_publish_history_skips_charged_ah_until_current_seen(
        subject, fake_role):
    """Regression guard for the Orion-TR honesty fix.

    The Orion-TR's DcDcConverterData decoder doesn't expose output
    current — _tick_history is called with current_a=None forever.
    /ChargedAh must NOT be written to D-Bus in that case, so the role's
    declared default (None) shows through and gui-v2 renders "--"
    rather than misleadingly charting 0 Ah.
    """
    # Simulate many ticks with no current data — must never set
    # _history_has_current_data and never write /ChargedAh.
    subject._tick_history(state=3, current_a=None)
    import time as _t
    subject._history_last_tick = _t.monotonic() - 5.0
    subject._tick_history(state=3, current_a=None)
    assert subject._history_has_current_data is False

    subject._publish_history(fake_role)
    assert "/History/Cumulative/User/OperationTime" in fake_role.values
    assert "/History/Cumulative/User/ChargedAh" not in fake_role.values

def test_publish_history_writes_charged_ah_after_first_real_current(
        subject, fake_role):
    """IP22-side path: as soon as we get a real current reading the
    flag flips and /ChargedAh starts being published, even on later
    ticks where current=None."""
    subject._tick_history(state=3, current_a=None)
    import time as _t
    subject._history_last_tick = _t.monotonic() - 1.0
    subject._tick_history(state=3, current_a=10.0)
    assert subject._history_has_current_data is True

    subject._history_op_time_s = 60.0
    subject._history_charged_ah = 0.5
    subject._publish_history(fake_role)
    assert fake_role.values["/History/Cumulative/User/ChargedAh"] == 0.5

    # Even after going back to None, /ChargedAh continues to publish
    # the latest accumulated value.  The flag is sticky.
    subject._history_last_tick = _t.monotonic() - 2.0
    subject._tick_history(state=3, current_a=None)
    fake_role.values.pop("/History/Cumulative/User/ChargedAh", None)
    subject._publish_history(fake_role)
    assert fake_role.values.get(
        "/History/Cumulative/User/ChargedAh") == 0.5

def test_publish_history_skips_charged_ah_settings_flush_too(
        subject, fake_role, fake_settings):
    """The persisted settings entry for ChargedAh must also be skipped
    when the device hasn't seen current — otherwise we'd persist a
    bogus 0 that survives a service restart."""
    subject._history_last_flush = 0.0  # force flush window
    subject._tick_history(state=3, current_a=None)
    subject._publish_history(fake_role)
    op_path = "/Settings/Devices/test_aabbccddeeff/History/OperationTime"
    ah_path = "/Settings/Devices/test_aabbccddeeff/History/ChargedAh"
    assert op_path in fake_settings.values
    assert ah_path not in fake_settings.values

def test_publish_history_throttles_settings_flush(subject, fake_role,
                                                   fake_settings):
    # First publish flushes (last_flush = 0).
    subject._history_op_time_s = 100.0
    subject._publish_history(fake_role)
    flushes_after_first = len(fake_settings.created)

    # Immediate second publish — within HISTORY_FLUSH_INTERVAL_S (60 s).
    subject._history_op_time_s = 200.0
    subject._publish_history(fake_role)
    assert len(fake_settings.created) == flushes_after_first

    # Force last-flush 61 s ago — next publish flushes again.
    subject._history_last_flush = time.monotonic() - 61.0
    subject._history_op_time_s = 300.0
    subject._publish_history(fake_role)
    op_path = "/Settings/Devices/test_aabbccddeeff/History/OperationTime"
    assert fake_settings.values[op_path] == 300.0

# ---------------------------------------------------------------------------
# Charger-side alarms
# ---------------------------------------------------------------------------

def test_publish_alarms_no_error_clears_all(subject, fake_role):
    # Pre-seed all paths to 2 to verify everything gets reset.
    for p in bcc.CHARGER_ALARM_PATHS:
        fake_role[p] = 2
    subject._publish_alarms(fake_role, error_code=0)
    for p in bcc.CHARGER_ALARM_PATHS:
        assert fake_role[p] == 0

def test_publish_alarms_battery_temp_does_not_set_alarm(subject, fake_role):
    for p in bcc.CHARGER_ALARM_PATHS:
        fake_role[p] = 0
    subject._publish_alarms(fake_role, error_code=1)   # TEMPERATURE_BATTERY_HIGH
    subject._publish_alarms(fake_role, error_code=14)  # TEMPERATURE_BATTERY_LOW
    for p in bcc.CHARGER_ALARM_PATHS:
        assert fake_role[p] == 0, (
            f"battery-temp errors should never assert {p} — they belong "
            "on a battery monitor, not on the charger")

@pytest.mark.parametrize("code,path,severity", [
    (2,  "/Alarms/HighVoltage",     2),
    (11, "/Alarms/HighRipple",      2),
    (17, "/Alarms/HighTemperature", 2),
    (24, "/Alarms/Fan",             2),
    (26, "/Alarms/HighTemperature", 2),
])
def test_publish_alarms_each_mapped_code(subject, fake_role,
                                         code, path, severity):
    for p in bcc.CHARGER_ALARM_PATHS:
        fake_role[p] = 0
    subject._publish_alarms(fake_role, error_code=code)
    assert fake_role[path] == severity
    # Every other path stays at 0.
    for p in bcc.CHARGER_ALARM_PATHS:
        if p != path:
            assert fake_role[p] == 0

def test_publish_alarms_transition_clears_previous(subject, fake_role):
    for p in bcc.CHARGER_ALARM_PATHS:
        fake_role[p] = 0
    subject._publish_alarms(fake_role, error_code=17)  # HighTemperature
    assert fake_role["/Alarms/HighTemperature"] == 2
    subject._publish_alarms(fake_role, error_code=2)   # HighVoltage
    assert fake_role["/Alarms/HighTemperature"] == 0   # cleared
    assert fake_role["/Alarms/HighVoltage"] == 2

def test_publish_alarms_missing_path_falls_back(subject, fake_role):
    # If somebody forgot to add_path("/Alarms/Fan"), the mixin must not
    # crash — only emit a debug log.
    class _MissingPathRoleService:
        values = {}

        def __setitem__(self, k, v):
            if k == "/Alarms/Fan":
                raise KeyError(k)
            self.values[k] = v

        def __getitem__(self, k):
            if k not in self.values:
                raise KeyError(k)
            return self.values[k]

    rs = _MissingPathRoleService()
    subject._publish_alarms(rs, error_code=24)  # FAN
    # No exception escaped; HighTemperature etc. (also missing) likewise.
    assert "/Alarms/Fan" not in rs.values

# ---------------------------------------------------------------------------
# DVCC engagement / /State override
# ---------------------------------------------------------------------------

def test_derive_published_state_off_stays_off(subject):
    subject._dvcc_engaged = True
    assert subject._derive_published_state(0) == 0

def test_derive_published_state_disengaged_passes_through(subject):
    subject._dvcc_engaged = False
    for s in (1, 3, 4, 5, 6, 7, 11, 247):
        assert subject._derive_published_state(s) == s

def test_derive_published_state_engaged_overrides(subject):
    subject._dvcc_engaged = True
    for s in (1, 3, 4, 5, 6, 7, 11, 247):
        assert subject._derive_published_state(s) == bcc.STATE_EXTERNAL_CONTROL

def test_set_dvcc_engaged_transitions_status(subject, fake_role):
    fake_role["/State"] = 4   # ABSORPTION
    subject._last_advertised_state = 4
    fake_role["/Link/NetworkStatus"] = 4

    subject._set_dvcc_engaged(fake_role, True)
    assert fake_role["/Link/NetworkStatus"] == 1
    assert fake_role["/State"] == bcc.STATE_EXTERNAL_CONTROL

    subject._set_dvcc_engaged(fake_role, False)
    assert fake_role["/Link/NetworkStatus"] == 4
    assert fake_role["/State"] == 4

def test_set_dvcc_engaged_off_device_stays_off(subject, fake_role):
    fake_role["/State"] = 0
    subject._last_advertised_state = 0
    fake_role["/Link/NetworkStatus"] = 4

    subject._set_dvcc_engaged(fake_role, True)
    assert fake_role["/Link/NetworkStatus"] == 1
    # Off stays off.
    assert fake_role["/State"] == 0

def test_set_dvcc_engaged_idempotent_when_unchanged(subject, fake_role):
    subject._last_advertised_state = 3
    fake_role["/State"] = bcc.STATE_EXTERNAL_CONTROL
    fake_role["/Link/NetworkStatus"] = 1
    subject._dvcc_engaged = True

    # Second engage call when already engaged — no spurious /State refresh.
    subject._set_dvcc_engaged(fake_role, True)
    assert fake_role["/State"] == bcc.STATE_EXTERNAL_CONTROL

def test_on_link_network_mode_engages_when_nonzero(subject, fake_role):
    fake_role["/Settings/BmsPresent"] = 0
    fake_role["/Link/NetworkStatus"] = 4
    subject._last_advertised_state = 4
    fake_role["/State"] = 4

    assert subject._on_link_network_mode_write(fake_role, 5) is True
    assert subject._dvcc_engaged is True
    assert fake_role["/Link/NetworkStatus"] == 1

def test_on_link_network_mode_zero_stays_engaged_if_bms_present(
        subject, fake_role):
    fake_role["/Settings/BmsPresent"] = 1
    fake_role["/Link/NetworkStatus"] = 1
    subject._dvcc_engaged = True

    subject._on_link_network_mode_write(fake_role, 0)
    # NetworkMode=0 alone doesn't disengage when BmsPresent=1.
    assert subject._dvcc_engaged is True

def test_on_settings_bms_present_triggers_engagement(subject, fake_role):
    fake_role["/Link/NetworkMode"] = 0
    fake_role["/Link/NetworkStatus"] = 4
    subject._last_advertised_state = 4
    fake_role["/State"] = 4

    subject._on_settings_bms_present_write(fake_role, 1)
    assert subject._dvcc_engaged is True
    assert fake_role["/Link/NetworkStatus"] == 1

    subject._on_settings_bms_present_write(fake_role, 0)
    assert subject._dvcc_engaged is False
    assert fake_role["/Link/NetworkStatus"] == 4

def test_on_link_passive_write_returns_true_unconditionally():
    # The default sense-input handler is a no-op store-only.
    assert bcc.ChargerCommonMixin._on_link_passive_write(None, 25.0) is True
    assert bcc.ChargerCommonMixin._on_link_passive_write(None, None) is True

# ---------------------------------------------------------------------------
# GATT write queue
# ---------------------------------------------------------------------------

def test_enqueue_drains_immediately_when_writer_idle(subject, writer):
    captured = []

    def cb(success):
        captured.append(success)

    subject._enqueue_write(0xEDF0, b"\xb4\x00", on_complete=cb)
    assert len(writer.calls) == 1
    assert writer.calls[0]["register_id"] == 0xEDF0
    assert writer.calls[0]["value_bytes"] == b"\xb4\x00"
    assert captured == [True]

def test_enqueue_collapses_repeat_writes_to_same_vreg(subject, writer):
    # Stall the writer so multiple enqueues queue up before drain.
    writer.busy = True
    subject._enqueue_write(0xEDF0, b"\xb4\x00")
    subject._enqueue_write(0xEDF0, b"\xfa\x00")
    subject._enqueue_write(0xEDF0, b"\x96\x00")
    # Three back-to-back enqueues to the same VREG → only the last one
    # should be in the slot map.
    assert subject._pending_writes[0xEDF0][0] == b"\x96\x00"

    # Now release the writer and let the drain run.
    writer.busy = False
    subject._kick_pending_writes()
    assert len(writer.calls) == 1
    assert writer.calls[0]["value_bytes"] == b"\x96\x00"

def test_enqueue_distinct_vregs_drain_serially(subject, writer):
    writer.busy = True
    subject._enqueue_write(0xEDF1, b"\xff")
    subject._enqueue_write(0xEDF7, b"\x18\x0b")
    assert set(subject._pending_writes) == {0xEDF1, 0xEDF7}

    writer.busy = False
    # First drain pops one entry.  Each on_done re-kicks via
    # _schedule_drain.  In tests the GLib stub doesn't actually fire
    # the timeout, so we kick manually to confirm both get out.
    subject._kick_pending_writes()
    assert len(writer.calls) == 1
    subject._kick_pending_writes()
    assert len(writer.calls) == 2
    written = {c["register_id"] for c in writer.calls}
    assert written == {0xEDF1, 0xEDF7}

def test_enqueue_when_busy_schedules_drain_retry(subject, writer):
    from gi.repository import GLib
    writer.busy = True
    subject._enqueue_write(0xEDF0, b"\xb4\x00")
    # Writer was busy → no immediate write.
    assert writer.calls == []
    # A drain retry was scheduled.
    assert len(GLib.scheduled) >= 1
    delay_ms, fn = GLib.scheduled[-1]
    assert delay_ms == bcc.PENDING_DRAIN_INTERVAL_MS

    # Releasing the writer + invoking the scheduled drain finishes the
    # pending write.
    writer.busy = False
    fn()
    assert len(writer.calls) == 1

def test_enqueue_failure_propagates_to_on_complete(subject, writer):
    writer.next_result = False
    captured = []
    subject._enqueue_write(0xEDF0, b"\xb4\x00",
                           on_complete=lambda ok: captured.append(ok))
    assert captured == [False]

def test_enqueue_does_not_double_schedule_drain(subject, writer):
    from gi.repository import GLib
    writer.busy = True
    subject._enqueue_write(0xEDF0, b"\xb4\x00")
    subject._enqueue_write(0xEDF7, b"\x18\x0b")
    # Even though we enqueued twice while busy, only one drain retry
    # is scheduled — _pending_drain_scheduled gates re-entry.
    assert len(GLib.scheduled) == 1
