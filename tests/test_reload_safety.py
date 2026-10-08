"""Reloads that never weaken control: bad files, moved sensors, timers and file polling."""

import json
import os
from pathlib import Path

from thermalctl.backend import FakeBackend
from thermalctl.config import load_effective
from thermalctl.controller import Controller
from thermalctl.safety import Reading

MAIN = """\
mode = "active"

[[zones]]
id = "cpu"
temperature_input = "temp"
temperature_curve = [[40, 20], [80, 100]]
hard_max_temp_c = 90
stale_after_s = 10

[[headers]]
id = "pwm1"
path = "/fake/pwm1"
mapped = true
min_duty = 40
min_duty_limit = 10
min_rpm = 300
stall_window_s = 15
zones = ["cpu"]
"""


def put(path, text):
    Path(path).write_text(text, encoding="ascii", newline="\n")


class Rig:
    def __init__(self, tmp_path, rpm=900.0):
        self.now = 1000.0
        self.main = tmp_path / "config.toml"
        self.over = tmp_path / "overrides.toml"
        self.status = tmp_path / "status.json"
        self.bump = 0
        put(self.main, MAIN)
        config, report = load_effective(self.main, self.over)
        self.backend = FakeBackend()
        self.backend.rpms = {"pwm1": rpm}
        self.ctl = Controller(
            config, self.backend, status_path=self.status, clock=lambda: self.now,
            hold_s=5.0, ema_alpha=1.0, ramp_down_per_s=2.0,
            overrides_path=self.over, config_path=self.main, overrides_report=report,
        )

    def cycle(self, advance=1.0):
        self.now += advance
        self.backend.inputs = {"temp": Reading(30.0, self.now)}
        self.ctl.cycle()
        return json.loads(self.status.read_text(encoding="utf-8"))

    def write(self, path, data):
        """Replace a file and move its mtime so the poll sees it on any file system."""
        if isinstance(data, bytes):
            Path(path).write_bytes(data)
        else:
            put(path, data)
        self.bump += 10
        os.utime(path, (1_000_000 + self.bump, 1_000_000 + self.bump))


def test_undecodable_config_on_reload_keeps_the_old_config_and_reports_it(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    before = rig.ctl.config
    rig.write(rig.main, b"mode = \"active\"\n\xff\xfe\x00garbage")
    doc = rig.cycle()
    assert rig.ctl.config is before
    assert doc["config_valid"] is False
    assert "UnicodeDecodeError" in doc["config_error"]
    assert doc["headers"]["pwm1"]["state"] == "failsafe"
    assert rig.backend.writes[-1] == ("pwm1", 100.0)
    # A good file clears the error, and the hold period still applies.
    rig.write(rig.main, MAIN)
    doc = rig.cycle()
    assert doc["config_valid"] is True and doc["config_error"] is None
    assert doc["headers"]["pwm1"]["state"] == "failsafe"


def test_oversized_number_in_config_is_rejected_not_raised(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.write(rig.main, MAIN.replace("stale_after_s = 10", "stale_after_s = " + "9" * 400))
    doc = rig.cycle()
    assert doc["config_valid"] is False and doc["config_error"]
    assert doc["headers"]["pwm1"]["state"] == "failsafe"


def test_non_configerror_in_overrides_keeps_the_previous_config(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.cycle()

    def boom(config, path):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("thermalctl.controller.apply_overrides", boom)
    rig.write(rig.over, "[headers.pwm1]\nmin_duty = 20\n")
    doc = rig.cycle()
    assert "UnicodeDecodeError" in doc["overrides_error"]
    assert doc["config_valid"] is True
    assert doc["headers"]["pwm1"]["state"] == "active"
    assert doc["headers"]["pwm1"]["min_duty"] == 40


def test_changed_temperature_input_is_refused_with_a_restart_required_status(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    before = rig.ctl.config
    rig.write(rig.main, MAIN.replace('temperature_input = "temp"', 'temperature_input = "other"'))
    for _ in range(5):
        doc = rig.cycle()
    assert rig.ctl.config is before
    assert doc["config_valid"] is True
    assert doc["config_error"].startswith("restart required")
    assert "other" in doc["config_error"]
    assert doc["headers"]["pwm1"]["state"] == "active"
    assert doc["headers"]["pwm1"]["reasons"] == []


def test_reload_that_keeps_the_sensor_still_applies_other_changes(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.write(rig.main, MAIN.replace("min_duty = 40", "min_duty = 50"))
    doc = rig.cycle()
    assert doc["config_error"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 50


def test_stall_timer_carries_across_reloads(tmp_path):
    rig = Rig(tmp_path, rpm=0.0)
    first = None
    # The floor flips every 4 s, well inside the 15 s stall window.
    for i in range(30):
        if i % 4 == 0:
            rig.write(rig.over, f"[headers.pwm1]\nmin_duty = {41 + (i // 4) % 2}\n")
        doc = rig.cycle()
        if first is None and doc["headers"]["pwm1"]["state"] == "failsafe":
            first = i
    assert first is not None and first <= 17
    assert "stall:pwm1" in doc["headers"]["pwm1"]["reasons"]
    assert rig.backend.writes[-1] == ("pwm1", 100.0)


def test_low_rpm_timer_carries_across_a_reload(tmp_path):
    rig = Rig(tmp_path, rpm=200.0)
    reasons = []
    for i in range(20):
        if i % 4 == 0:
            rig.write(rig.over, f"[headers.pwm1]\nmin_duty = {61 + (i // 4) % 2}\n")
        reasons = rig.cycle()["headers"]["pwm1"]["reasons"]
    assert "low_rpm:pwm1" in reasons


def test_editing_the_main_config_is_picked_up_without_sighup(tmp_path):
    rig = Rig(tmp_path)
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 40
    assert rig.ctl.reload_requested is False
    rig.write(rig.main, MAIN.replace("min_duty = 40", "min_duty = 55"))
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["min_duty"] == 55
    assert doc["headers"]["pwm1"]["duty"] == 55
    # An unchanged file is not read again.
    calls = []
    original = rig.ctl.reload
    rig.ctl.reload = lambda path: calls.append(path) or original(path)
    rig.cycle()
    assert calls == []
