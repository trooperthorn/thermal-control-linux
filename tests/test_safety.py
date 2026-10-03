import math

import pytest

from thermalctl.config import Header, Zone
from thermalctl.safety import (
    ACTIVE,
    DRY_RUN,
    FAILSAFE,
    HeaderSafety,
    Reading,
    failsafe_duty,
)
from thermalctl.smoothing import Ema, OutputShaper, apply_floor

CURVE = ((40.0, 20.0), (80.0, 100.0))
ZONE = Zone("cpu", "temp", CURVE, 90.0, 10.0, "load", ((0.0, 0.0), (100.0, 100.0)))
HEADER = Header("pwm1", "/fake/pwm1", True, 20.0, 300, 15.0, ("cpu",))


def good(now, temp=50.0, load=10.0):
    return {"temp": Reading(temp, now), "load": Reading(load, now)}


def machine(enabled=True, hold=30.0):
    return HeaderSafety(HEADER, hold, enabled)


def step(m, now, readings=None, rpm=900.0, commanded=50.0, **kw):
    return m.update(
        now,
        zones=[ZONE],
        readings=good(now) if readings is None else readings,
        rpm=rpm,
        commanded=commanded,
        **kw,
    )


def test_healthy_states():
    assert step(machine(), 0) == ACTIVE
    assert step(machine(enabled=False), 0) == DRY_RUN
    unmapped = HeaderSafety(Header("p", "/f", False, 20, 300, 15, ("cpu",)), 30, True)
    assert unmapped.healthy_state == DRY_RUN


@pytest.mark.parametrize(
    "readings,reason",
    [
        ({"load": Reading(1.0, 0)}, "missing_input:temp"),
        ({"temp": Reading(None, None), "load": Reading(1.0, 0)}, "missing_input:temp"),
        ({"temp": Reading(math.nan, 0), "load": Reading(1.0, 0)}, "invalid_input:temp"),
        ({"temp": Reading(math.inf, 0), "load": Reading(1.0, 0)}, "invalid_input:temp"),
        ({"temp": Reading(50.0, -50), "load": Reading(1.0, 0)}, "stale_input:temp"),
        ({"temp": Reading(50.0, 0), "load": Reading(1.0, -50)}, "stale_input:load"),
        ({"temp": Reading(50.0, 0)}, "missing_input:load"),
        ({"temp": Reading(95.0, 0), "load": Reading(1.0, 0)}, "over_temp:cpu"),
    ],
)
def test_input_causes(readings, reason):
    m = machine()
    assert step(m, 0, readings) == FAILSAFE
    assert reason in m.reasons


def test_future_timestamp_is_stale():
    m = machine()
    readings = {"temp": Reading(50.0, 100.0), "load": Reading(1.0, 0)}
    assert step(m, 0, readings) == FAILSAFE
    assert "stale_input:temp" in m.reasons


def test_stall_after_window():
    m = machine()
    assert step(m, 0, rpm=0.0) == ACTIVE
    assert step(m, 15, rpm=0.0) == ACTIVE
    assert step(m, 16, rpm=0.0) == FAILSAFE
    assert "stall:pwm1" in m.reasons


def test_no_stall_at_floor_or_with_rpm():
    m = machine()
    for t in range(0, 100, 5):
        assert step(m, t, rpm=0.0, commanded=20.0) == ACTIVE
    m2 = machine()
    step(m2, 0, rpm=0.0)
    step(m2, 10, rpm=500.0)
    assert step(m2, 20, rpm=0.0) == ACTIVE  # timer restarted


def test_unreadable_rpm_is_failsafe():
    assert step(machine(), 0, rpm=None) == FAILSAFE
    assert step(machine(), 0, rpm=math.nan) == FAILSAFE


def test_invalid_config_and_exit():
    m = machine()
    assert step(m, 0, config_valid=False) == FAILSAFE
    assert "invalid_config" in m.reasons
    m2 = machine()
    m2.request_exit()
    assert step(m2, 0) == FAILSAFE
    assert "exiting" in m2.reasons
    assert step(m2, 1000) == FAILSAFE  # exit never clears


def test_hold_period_prevents_flapping():
    m = machine(hold=30.0)
    bad = {"load": Reading(1.0, 0)}
    assert step(m, 0, bad) == FAILSAFE
    assert step(m, 1, good(1)) == FAILSAFE
    assert step(m, 20, good(20)) == FAILSAFE
    # a new fault inside the hold restarts it
    assert step(m, 25, bad) == FAILSAFE
    assert step(m, 26, good(26)) == FAILSAFE
    assert step(m, 55, good(55)) == FAILSAFE
    assert step(m, 56, good(56)) == ACTIVE
    assert m.reasons == ()


def test_reasons_kept_through_hold_and_change_time():
    m = machine()
    step(m, 5, {"load": Reading(1.0, 0)})
    assert m.last_change == 5
    assert m.reasons
    step(m, 6)
    assert m.last_change == 5
    assert m.reasons


def test_failsafe_duty():
    assert failsafe_duty(False) == 100.0
    assert failsafe_duty(True) == 0.0


def test_ema():
    e = Ema(0.5)
    assert e.update(10) == 10
    assert e.update(20) == 15
    e.reset()
    assert e.update(40) == 40
    with pytest.raises(ValueError):
        Ema(0)
    with pytest.raises(ValueError):
        e.update(math.nan)


def test_spin_up_immediate_spin_down_ramped():
    s = OutputShaper(hysteresis=2.0, ramp_down_per_s=5.0)
    assert s.apply(30, 1) == 30
    assert s.apply(90, 0.001) == 90  # immediate regardless of dt
    assert s.apply(40, 1) == 85
    assert s.apply(40, 2) == 75
    assert s.apply(40, 100) == 40


def test_hysteresis_holds_small_decreases():
    s = OutputShaper(hysteresis=5.0, ramp_down_per_s=100.0)
    s.apply(50, 1)
    assert s.apply(47, 1) == 50
    assert s.apply(45, 1) == 45


def test_hysteresis_never_below_curve():
    s = OutputShaper(hysteresis=5.0, ramp_down_per_s=1.0)
    s.apply(80, 1)
    for target in [78, 70, 60, 74, 90, 20, 22, 21]:
        assert s.apply(target, 1.0) >= target


def test_floors():
    assert apply_floor(5, 20) == 20
    assert apply_floor(0, 20) == 20
    assert apply_floor(50, 20) == 50
    assert apply_floor(150, 20) == 100
    assert apply_floor(0, 20, allow_zero=True) == 0
    assert apply_floor(5, 20, allow_zero=True) == 20
    assert apply_floor(math.nan, 20) == 100
