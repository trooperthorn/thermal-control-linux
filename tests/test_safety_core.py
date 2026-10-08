"""Regression tests for the October audit: paths that could leave a fan running slow."""

from dataclasses import replace

from thermalctl.backend import FakeBackend
from thermalctl.backends.sysfs import SysfsBackend
from thermalctl.config import parse_config
from thermalctl.controller import Controller
from thermalctl.safety import (
    ACTIVE,
    FAILSAFE,
    FAILSAFE_WRITE_FAILED,
    HeaderSafety,
    Reading,
)
from test_safety import HEADER, ZONE, step

ZONE_CFG = {
    "id": "cpu",
    "temperature_input": "temp",
    "temperature_curve": [[40, 20], [80, 100]],
    "hard_max_temp_c": 90,
    "stale_after_s": 10,
}


def header_cfg(hid):
    return {
        "id": hid, "path": "/fake/" + hid, "mapped": True, "min_duty": 20,
        "min_rpm": 300, "stall_window_s": 15, "zones": ["cpu"],
    }


def make_controller(tmp_path, backend, headers=("pwm1",), alpha=0.5):
    config = parse_config(
        {"mode": "active", "zones": [ZONE_CFG], "headers": [header_cfg(h) for h in headers]}
    )
    now = {"t": 1000.0}
    ctl = Controller(
        config, backend, status_path=tmp_path / "status.json",
        clock=lambda: now["t"], hold_s=5.0, ema_alpha=alpha, hysteresis=0.0,
    )

    def cycle(temp):
        now["t"] += 2.0
        backend.inputs = {"temp": Reading(temp, now["t"])}
        backend.rpms = {h: 900.0 for h in headers}
        return ctl.cycle()

    return ctl, cycle


def test_four_headers_on_one_zone_get_one_smoothed_value(tmp_path):
    ctl, cycle = make_controller(
        tmp_path, FakeBackend(), headers=("pwm1", "pwm2", "pwm3", "pwm4")
    )
    cycle(40.0)
    doc = cycle(80.0)
    # One update at alpha 0.5 moves the average from 40 to 60, which the curve maps to 60
    # percent. Four updates in one cycle would have moved it to 77.5 and given 94 percent.
    assert ctl.emas[("cpu", "temp")].value == 60.0
    duties = {doc["headers"][f"pwm{n}"]["duty"] for n in (1, 2, 3, 4)}
    assert duties == {60.0}


def test_failed_full_speed_write_hands_header_to_firmware(tmp_path):
    class Failing(FakeBackend):
        def write_duty(self, header_id, duty):
            if duty == 100.0:
                raise OSError(5, "Input/output error")
            super().write_duty(header_id, duty)

    backend = Failing()
    ctl, cycle = make_controller(tmp_path, backend, headers=("pwm1", "pwm2"))
    cycle(50.0)
    doc = cycle(120.0)  # implausible temperature forces failsafe on both headers
    assert backend.releases == ["pwm1", "pwm2"]
    for hid in ("pwm1", "pwm2"):
        h = doc["headers"][hid]
        assert h["state"] == FAILSAFE
        assert FAILSAFE_WRITE_FAILED in h["notes"]
        assert h["duty"] == 0.0


def test_failed_full_speed_write_leads_to_enable_five_on_sysfs(tmp_path):
    tree = tmp_path / "hw"
    tree.mkdir()
    (tree / "pwm1").write_text("60\n", encoding="ascii")
    (tree / "pwm1_enable").write_text("5\n", encoding="ascii")
    (tree / "fan1_input").write_text("900\n", encoding="ascii")
    (tree / "temp1_input").write_text("50000\n", encoding="ascii")
    now = {"t": 1000.0}
    sysfs = SysfsBackend(
        {"pwm1": str(tree / "pwm1")}, {"temp": str(tree / "temp1_input")}, ["pwm1"],
        tmp_path / "state.json", active=True, clock=lambda: now["t"],
    )
    sysfs.start()
    config = parse_config(
        {"mode": "active", "zones": [ZONE_CFG], "headers": [header_cfg("pwm1")]}
    )
    ctl = Controller(config, sysfs, status_path=tmp_path / "status.json", clock=lambda: now["t"])
    now["t"] += 2.0
    ctl.cycle()
    assert (tree / "pwm1_enable").read_text().strip() == "1"
    real = sysfs.write_duty

    def failing(header_id, duty):
        if duty >= 100.0:
            raise OSError(5, "Input/output error")
        real(header_id, duty)

    sysfs.write_duty = failing
    (tree / "temp1_input").write_text("0\n", encoding="ascii")  # dead sensor
    now["t"] += 2.0
    doc = ctl.cycle()
    assert doc["headers"]["pwm1"]["state"] == FAILSAFE
    assert (tree / "pwm1_enable").read_text().strip() == "5"
    assert FAILSAFE_WRITE_FAILED in doc["headers"]["pwm1"]["notes"]


def test_fan_at_50_rpm_at_the_floor_trips_failsafe():
    m = HeaderSafety(HEADER, 30.0, True)
    # 20 percent is below min_rpm_duty, so the min_rpm check does not apply there.
    assert step(m, 0, rpm=50.0, commanded=20.0) == ACTIVE
    assert step(m, 15, rpm=50.0, commanded=20.0) == ACTIVE
    assert step(m, 16, rpm=50.0, commanded=20.0) == FAILSAFE
    assert "slow_fan:pwm1" in m.reasons


def test_slow_fan_timer_restarts_when_speed_returns():
    m = HeaderSafety(HEADER, 30.0, True)
    step(m, 0, rpm=50.0, commanded=20.0)
    step(m, 10, rpm=400.0, commanded=20.0)
    assert step(m, 20, rpm=50.0, commanded=20.0) == ACTIVE


def test_healthy_idle_fan_and_min_rpm_zero_are_not_slow():
    m = HeaderSafety(HEADER, 30.0, True)
    for t in range(0, 100, 5):
        assert step(m, t, rpm=400.0, commanded=20.0) == ACTIVE
    free = HeaderSafety(replace(HEADER, min_rpm=0), 30.0, True)
    for t in range(0, 100, 5):
        assert step(free, t, rpm=50.0, commanded=20.0) == ACTIVE
