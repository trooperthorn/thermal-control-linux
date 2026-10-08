"""Reloading the overrides file while the controller runs."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from thermalctl import config as config_module
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


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    """Make the overrides file look root-owned and private on every platform."""
    monkeypatch.setattr(config_module, "_posix", lambda: True)

    def fake_stat(path):
        os.stat(path)  # a missing file must still raise FileNotFoundError
        return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)

    monkeypatch.setattr(config_module, "_stat_file", fake_stat)


def put(path, text):
    Path(path).write_text(text, encoding="ascii", newline="\n")


class Rig:
    def __init__(self, tmp_path, overrides_text=None, main_text=MAIN):
        self.now = 1000.0
        self.main = tmp_path / "config.toml"
        self.over = tmp_path / "overrides.toml"
        self.status = tmp_path / "status.json"
        put(self.main, main_text)
        if overrides_text is not None:
            put(self.over, overrides_text)
        config, report = load_effective(self.main, self.over)
        self.backend = FakeBackend()
        self.backend.rpms = {"pwm1": 900.0}
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

    def write_overrides(self, text):
        put(self.over, text)
        # Move the mtime so the change is seen however coarse the file system clock is.
        self._bump = getattr(self, "_bump", 0) + 10
        stamp = self._bump
        os.utime(self.over, (1_000_000 + stamp, 1_000_000 + stamp))


def test_status_has_override_fields_and_effective_floor(tmp_path):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 25\n")
    doc = rig.cycle()
    assert doc["overrides_applied"] is True
    assert doc["overrides_error"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 25


def test_status_without_overrides(tmp_path):
    doc = Rig(tmp_path).cycle()
    assert doc["overrides_applied"] is False
    assert doc["overrides_error"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 40


def test_floor_change_applies_on_file_change_with_ramp(tmp_path):
    rig = Rig(tmp_path)
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 40
    rig.write_overrides("[headers.pwm1]\nmin_duty = 20\n")
    doc = rig.cycle()
    assert doc["overrides_applied"] is True
    assert doc["headers"]["pwm1"]["min_duty"] == 20
    # Ramped, not dropped: 2 percent per second from 40.
    assert doc["headers"]["pwm1"]["duty"] == 38
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 36
    for _ in range(12):
        doc = rig.cycle()
    assert doc["headers"]["pwm1"]["duty"] == 20


def test_raising_the_floor_applies_at_once(tmp_path):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 20\n")
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 20
    rig.write_overrides("[headers.pwm1]\nmin_duty = 35\n")
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 35


def test_sighup_reloads_without_a_file_change(tmp_path):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 30\n")
    rig.cycle()
    # Same size and a pinned mtime: only the request can trigger the reload.
    put(rig.over, "[headers.pwm1]\nmin_duty = 25\n")
    os.utime(rig.over, ns=(rig.ctl._stamps[1][0],) * 2)
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 30
    rig.ctl.request_reload()
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 25
    assert rig.ctl.reload_requested is False


def test_removing_the_file_returns_to_the_main_floor(tmp_path):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    rig.over.unlink()
    doc = rig.cycle()
    assert doc["overrides_applied"] is False
    assert doc["headers"]["pwm1"]["min_duty"] == 40


@pytest.mark.parametrize(
    "bad",
    [
        "[headers.pwm1]\nmin_duty = 5\n",
        "[headers.pwm9]\nmin_duty = 20\n",
        "mode = [\n",
    ],
)
def test_bad_override_keeps_previous_values_and_sets_error(tmp_path, bad):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 25\n")
    rig.cycle()
    rig.write_overrides(bad)
    doc = rig.cycle()
    assert doc["overrides_error"]
    assert doc["overrides_applied"] is True
    assert doc["headers"]["pwm1"]["min_duty"] == 25
    assert doc["headers"]["pwm1"]["state"] == "active"
    assert doc["config_valid"] is True
    # A good file clears the error.
    rig.write_overrides("[headers.pwm1]\nmin_duty = 30\n")
    doc = rig.cycle()
    assert doc["overrides_error"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 30


def test_bad_override_reason_names_the_problem(tmp_path):
    rig = Rig(tmp_path)
    rig.write_overrides("[headers.pwm1]\nmin_duty = 5\n")
    assert "below the allowed minimum" in rig.cycle()["overrides_error"]


def test_mode_change_at_runtime_is_refused_and_applied_after_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.write_overrides('mode = "dry_run"\n[headers.pwm1]\nmin_duty = 20\n')
    doc = rig.cycle()
    assert doc["mode"] == "active"
    assert "needs a restart" in doc["overrides_error"]
    # The refused file changes nothing, including its floor.
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    # A restart builds a new controller from the merged config and applies both.
    config, report = load_effective(rig.main, rig.over)
    restarted = Controller(
        config, FakeBackend(), status_path=rig.status, clock=lambda: rig.now,
        overrides_path=rig.over, config_path=rig.main, overrides_report=report,
    )
    restarted.backend.rpms = {"pwm1": 900.0}
    restarted.backend.inputs = {"temp": Reading(30.0, rig.now)}
    doc = restarted.cycle()
    assert doc["mode"] == "dry_run"
    assert doc["overrides_applied"] is True and doc["overrides_error"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 20
    # The restarted controller does not treat its own mode as a runtime change.
    assert restarted.reload(rig.main) is True


def test_ignored_overrides_at_start_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config_module, "_stat_file",
        lambda path: SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o600),
    )
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 20\n")
    doc = rig.cycle()
    assert doc["overrides_applied"] is False
    assert "not owned by root" in doc["overrides_error"]
    assert doc["headers"]["pwm1"]["min_duty"] == 40


def test_main_config_is_never_rewritten(tmp_path):
    rig = Rig(tmp_path)
    before = rig.main.read_bytes()
    rig.write_overrides("[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    assert rig.main.read_bytes() == before
