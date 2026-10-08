"""Monotonic timers, the frozen sensor rule, and validation of every numeric input."""

import copy
import json
import time

import pytest

from test_cli import make_tree, put, write_config, write_proc_stat
from thermalctl import cli
from thermalctl.__main__ import main
from thermalctl.backend import FakeBackend
from thermalctl.backends.sysfs import SysfsBackend
from thermalctl.config import ConfigError, check_unique_paths, parse_config
from thermalctl.controller import (
    Controller, remove_stale_status_temps, strict_json_safe, write_status_atomic,
)
from thermalctl.hwmon import HwmonError, resolve_config
from thermalctl.load import LoadBackend
from thermalctl.safety import Reading

ZONE = {
    "id": "cpu", "temperature_input": "temp", "temperature_curve": [[40, 20], [80, 100]],
    "hard_max_temp_c": 90, "stale_after_s": 10,
}
HEADER = {
    "id": "pwm1", "path": "/fake/pwm1", "mapped": True, "min_duty": 20,
    "min_rpm": 300, "stall_window_s": 15, "zones": ["cpu"],
}


def strict_loads(text):
    def refuse(token):
        raise ValueError(f"non-strict JSON constant {token}")

    return json.loads(text, parse_constant=refuse)


class Rig:
    """A controller whose timer clock and wall clock can be moved independently."""

    def __init__(self, tmp_path, hold_s=5.0):
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        self.backend = FakeBackend()
        self.status = tmp_path / "status.json"
        self.rpm = 900.0
        self.temp = 50.0
        self.ctl = Controller(
            parse_config({"mode": "active", "zones": [ZONE], "headers": [HEADER]}),
            self.backend, status_path=self.status, clock=lambda: self.mono,
            wall_clock=lambda: self.wall, hold_s=hold_s, ema_alpha=1.0,
        )

    def cycle(self, advance=1.0):
        self.mono += advance
        self.wall += advance
        self.backend.inputs = {"temp": Reading(self.temp, self.mono)}
        self.backend.rpms = {"pwm1": self.rpm}
        return self.ctl.cycle()


# -- timers run on the monotonic clock ---------------------------------------------------


def test_default_clocks_are_monotonic(tmp_path):
    ctl = Controller(
        parse_config({"mode": "dry_run", "zones": [ZONE], "headers": [HEADER]}),
        FakeBackend(), status_path=tmp_path / "s.json",
    )
    assert ctl.clock is time.monotonic
    assert ctl.wall_clock is time.time
    assert SysfsBackend({}, {}, [], tmp_path / "st.json").clock is time.monotonic
    assert LoadBackend(FakeBackend(), str(tmp_path / "absent")).clock is time.monotonic


@pytest.mark.parametrize("step", [-3600.0, 3600.0])
def test_wall_clock_step_does_not_disable_the_stall_timer(tmp_path, step):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.rpm = 0.0
    rig.cycle()          # the stall timer starts
    rig.wall += step     # NTP step, RTC-less boot, VM resume
    states = [rig.cycle()["headers"]["pwm1"]["state"] for _ in range(20)]
    # The window is 15 s of monotonic time, so the fan is condemned within 17 cycles.
    assert states[0] == "active"
    assert "failsafe" in states[:17]


def test_wall_clock_step_forward_does_not_finish_the_hold(tmp_path):
    rig = Rig(tmp_path, hold_s=30.0)
    rig.cycle()
    rig.temp = 0.0       # an exact zero is a dead sensor
    assert rig.cycle()["headers"]["pwm1"]["state"] == "failsafe"
    rig.temp = 50.0
    assert rig.cycle()["headers"]["pwm1"]["state"] == "failsafe"   # the hold starts
    rig.wall += 3600.0
    assert rig.cycle(advance=1.0)["headers"]["pwm1"]["state"] == "failsafe"


def test_wall_clock_step_back_does_not_extend_the_hold(tmp_path):
    rig = Rig(tmp_path, hold_s=5.0)
    rig.cycle()
    rig.temp = 0.0
    rig.cycle()
    rig.temp = 50.0
    rig.cycle()
    rig.wall -= 3600.0
    states = [rig.cycle()["headers"]["pwm1"]["state"] for _ in range(8)]
    assert states[-1] == "active"


def test_status_timestamps_stay_wall_clock(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.temp = 0.0
    doc = rig.cycle()
    assert doc["timestamp"] == rig.wall
    assert doc["headers"]["pwm1"]["last_change"] == rig.wall   # failsafe entered this cycle
    rig.wall += 100.0
    rig.mono += 100.0
    rig.temp = 50.0
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["last_change"] == pytest.approx(rig.wall - 101.0)


# -- the frozen sensor rule --------------------------------------------------------------


def _sysfs_rig(tmp_path):
    tree = make_tree(tmp_path)
    now = {"t": 500.0}
    sensor = str(tree / "temp1_input")
    backend = SysfsBackend(
        {"p1": str(tree / "pwm1")}, {sensor: sensor}, [], tmp_path / "state.json",
        clock=lambda: now["t"],
    )
    config = parse_config({
        "mode": "dry_run",
        "zones": [dict(ZONE, temperature_input=sensor)],
        "headers": [dict(HEADER, id="p1", path=str(tree / "pwm1"), mapped=False)],
    })
    ctl = Controller(config, backend, status_path=tmp_path / "status.json",
                     clock=lambda: now["t"])
    return tree, now, backend, ctl


def test_reading_is_stamped_with_the_read_time_and_the_last_change(tmp_path):
    tree, now, backend, _ = _sysfs_rig(tmp_path)
    sensor = str(tree / "temp1_input")
    assert backend.read_inputs()[sensor] == Reading(45.5, 500.0, unchanged_since=500.0)
    now["t"] = 530.0
    assert backend.read_inputs()[sensor] == Reading(45.5, 530.0, unchanged_since=500.0)
    put(tree / "temp1_input", "46000\n")
    assert backend.read_inputs()[sensor] == Reading(46.0, 530.0, unchanged_since=530.0)


def test_frozen_sensor_trips_after_frozen_after_s_not_stale_after_s(tmp_path):
    tree, now, _, ctl = _sysfs_rig(tmp_path)
    frozen_after = int(ctl.config.zones[0].frozen_after_s)
    assert frozen_after == 900 and ctl.config.zones[0].stale_after_s == 10
    assert ctl.cycle()["headers"]["p1"]["state"] == "dry_run"
    states = []
    for _ in range(frozen_after + 5):    # the value never changes
        now["t"] += 1.0
        doc = ctl.cycle()
        states.append(doc["headers"]["p1"]["state"])
    # Far past stale_after_s the value is still trusted; only frozen_after_s trips it.
    assert states[:frozen_after] == ["dry_run"] * frozen_after
    assert states[-1] == "failsafe"
    assert any(r.startswith("frozen_input:") for r in doc["headers"]["p1"]["reasons"])


def test_sensor_stuck_for_frozen_after_s_trips(tmp_path):
    tree, now, _, ctl = _sysfs_rig(tmp_path)
    ctl.cycle()
    now["t"] += 899.0
    assert ctl.cycle()["headers"]["p1"]["state"] == "dry_run"
    now["t"] += 2.0
    doc = ctl.cycle()
    assert doc["headers"]["p1"]["state"] == "failsafe"
    assert doc["headers"]["p1"]["reasons"] == ["frozen_input:" + str(tree / "temp1_input")]


def test_one_degree_sensor_stepping_every_30s_stays_on_the_example_curve(tmp_path):
    import tomllib

    from test_config import EXAMPLE

    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    tree = make_tree(tmp_path)
    sensor = str(tree / "temp1_input")
    zone = dict(raw["zones"][0], temperature_input=sensor)
    zone.pop("load_input")
    zone.pop("load_curve")
    now = {"t": 500.0}
    backend = SysfsBackend(
        {"p1": str(tree / "pwm1")}, {sensor: sensor}, [], tmp_path / "state.json",
        clock=lambda: now["t"],
    )
    config = parse_config({
        "mode": "dry_run", "zones": [zone],
        "headers": [dict(HEADER, id="p1", path=str(tree / "pwm1"), mapped=False)],
    })
    ctl = Controller(config, backend, status_path=tmp_path / "status.json",
                     clock=lambda: now["t"])
    for i in range(1800):
        now["t"] += 1.0
        if i % 30 == 0:
            put(tree / "temp1_input", f"{45000 + 1000 * ((i // 30) % 2)}\n")
        doc = ctl.cycle()
        assert doc["headers"]["p1"]["state"] == "dry_run", (i, doc["headers"]["p1"]["reasons"])


def test_reader_that_stops_reading_is_still_stale(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.mono += 11.0
    rig.backend.inputs = {"temp": Reading(50.0, rig.mono - 11.0)}
    doc = rig.ctl.cycle()
    assert any(r.startswith("stale_input:") for r in doc["headers"]["pwm1"]["reasons"])


def test_sensor_that_keeps_changing_never_goes_stale(tmp_path):
    tree, now, _, ctl = _sysfs_rig(tmp_path)
    for i in range(40):
        now["t"] += 1.0
        put(tree / "temp1_input", f"{45000 + 100 * (i % 2)}\n")
        assert ctl.cycle()["headers"]["p1"]["state"] == "dry_run"


def test_unreadable_sensor_forgets_its_history(tmp_path):
    tree, now, backend, _ = _sysfs_rig(tmp_path)
    sensor = str(tree / "temp1_input")
    backend.read_inputs()
    (tree / "temp1_input").unlink()
    assert backend.read_inputs()[sensor] == Reading(None, None)
    put(tree / "temp1_input", "45500\n")
    now["t"] = 900.0
    assert backend.read_inputs()[sensor] == Reading(45.5, 900.0, unchanged_since=900.0)


# -- /proc/stat counter resets ------------------------------------------------------------


def test_counter_reset_reports_no_load_instead_of_a_stale_value(tmp_path):
    proc = write_proc_stat(tmp_path, 100, 900)
    now = {"t": 10.0}
    backend = LoadBackend(FakeBackend(), str(proc), clock=lambda: now["t"])
    put(proc, "cpu  200 0 0 1000 0 0 0 0 0 0\n")
    now["t"] = 12.0
    backend.read_inputs()                      # warm-up
    put(proc, "cpu  300 0 0 1100 0 0 0 0 0 0\n")
    now["t"] = 14.0
    assert backend.read_inputs()["cpu_load_percent"] == Reading(50.0, 14.0)
    put(proc, "cpu  10 0 0 20 0 0 0 0 0 0\n")  # the counters went backwards
    now["t"] = 16.0
    assert backend.read_inputs()["cpu_load_percent"] == Reading(None, None)
    put(proc, "cpu  110 0 0 120 0 0 0 0 0 0\n")
    now["t"] = 18.0
    assert backend.read_inputs()["cpu_load_percent"] == Reading(50.0, 18.0)


def test_unchanged_counters_keep_the_old_timestamp(tmp_path):
    proc = write_proc_stat(tmp_path, 100, 900)
    now = {"t": 10.0}
    backend = LoadBackend(FakeBackend(), str(proc), clock=lambda: now["t"])
    put(proc, "cpu  200 0 0 1000 0 0 0 0 0 0\n")
    now["t"] = 12.0
    backend.read_inputs()
    put(proc, "cpu  300 0 0 1100 0 0 0 0 0 0\n")
    now["t"] = 14.0
    backend.read_inputs()
    now["t"] = 40.0                            # the counters did not move for 26 s
    assert backend.read_inputs()["cpu_load_percent"].timestamp == 14.0


# -- interval validation ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "0.001", "1e9"])
def test_run_rejects_unusable_interval(tmp_path, capsys, value):
    assert main(["run", "--config", str(tmp_path / "c.toml"), f"--interval={value}"]) == 2
    assert "--interval" in capsys.readouterr().err


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, 1e9])
def test_run_service_rejects_unusable_interval(tmp_path, value):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    code = cli.run_service(
        str(config), str(tmp_path / "s.json"), str(tmp_path / "st.json"), value,
        should_stop=lambda: True, sleep=lambda s: None,
    )
    assert code == 2
    assert not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("value", ["nan", "-1"])
def test_status_rejects_unusable_max_age(tmp_path, value):
    with pytest.raises(SystemExit) as info:
        main(["status", "--status-path", str(tmp_path / "s.json"), "--max-age", value])
    assert info.value.code == 2


def test_config_rejects_non_finite_timer_values():
    for key, where in (("stale_after_s", "zones"), ("stall_window_s", "headers")):
        for bad in (float("nan"), float("inf"), 0, -5):
            data = {"mode": "dry_run", "zones": [dict(ZONE)], "headers": [dict(HEADER)]}
            data[where][0][key] = bad
            with pytest.raises(ConfigError):
                parse_config(data)


# -- duplicate pwm paths ------------------------------------------------------------------


def _two_headers(path_a, path_b):
    return {
        "mode": "dry_run", "zones": [ZONE],
        "headers": [dict(HEADER, id="a", path=path_a), dict(HEADER, id="b", path=path_b)],
    }


def test_duplicate_pwm_path_is_refused_at_load():
    with pytest.raises(ConfigError, match="same pwm file"):
        parse_config(_two_headers("/sys/class/hwmon/hwmon1/pwm1", "/sys/class/hwmon/hwmon1/pwm1"))


def test_duplicate_pwm_path_is_refused_after_normalising():
    with pytest.raises(ConfigError, match="same pwm file"):
        parse_config(_two_headers("/sys/hw/hwmon1/pwm1", "/sys/hw/./hwmon1//pwm1"))


def test_distinct_pwm_paths_are_accepted():
    parse_config(_two_headers("/sys/hw/hwmon1/pwm1", "/sys/hw/hwmon1/pwm2"))
    check_unique_paths(())


def test_chip_reference_and_its_real_path_are_one_file(tmp_path):
    chip = tmp_path / "hwmon3"
    chip.mkdir()
    put(chip / "name", "nct6779\n")
    put(chip / "pwm1", "100\n")
    config = parse_config(_two_headers("nct6779:pwm1", (chip / "pwm1").as_posix()))
    with pytest.raises(HwmonError, match="same pwm file"):
        resolve_config(config, tmp_path)


def test_check_config_refuses_duplicate_pwm_path(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    text = config.read_text(encoding="utf-8").replace('/pwm2"', '/pwm1"').replace('id = "pwm2"', 'id = "pwm2b"')
    config.write_text(text, encoding="utf-8")
    assert main(["check-config", str(config), "--overrides", str(tmp_path / "none.toml")]) == 1
    assert "same pwm file" in capsys.readouterr().err


# -- status file --------------------------------------------------------------------------


def test_stale_status_temp_files_are_removed_at_start(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    status = tmp_path / "status.json"
    leftovers = [tmp_path / "status.json.ab12cd.tmp", tmp_path / "status.json.zz99.tmp"]
    keep = [tmp_path / "other.json.ab12cd.tmp", tmp_path / "status.json.note"]
    for path in leftovers + keep:
        put(path, "{partial")
    put(status, '{"timestamp": 1.0}\n')
    proc = write_proc_stat(tmp_path, 100, 900)
    code = cli.run_service(
        str(config), str(status), str(tmp_path / "state.json"), 1.0,
        should_stop=lambda: True, sleep=lambda s: None, proc_stat=str(proc),
    )
    assert code == 0
    assert not any(p.exists() for p in leftovers)
    assert all(p.exists() for p in keep)
    assert status.exists()


def test_remove_stale_status_temps_leaves_the_status_file(tmp_path):
    status = tmp_path / "status.json"
    put(status, "{}\n")
    put(tmp_path / "status.json.q.tmp", "x")
    assert [p.name for p in remove_stale_status_temps(status)] == ["status.json.q.tmp"]
    assert status.exists()
    assert remove_stale_status_temps(tmp_path / "missing_dir" / "status.json") == []


def test_status_json_is_strict_with_nan_inputs(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.temp = float("nan")
    rig.rpm = float("nan")
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["state"] == "failsafe"
    parsed = strict_loads(rig.status.read_text(encoding="utf-8"))
    assert parsed["zones"]["cpu"]["temperature"] is None
    assert parsed["headers"]["pwm1"]["rpm"] is None


def test_write_status_atomic_never_writes_non_finite_numbers(tmp_path):
    target = tmp_path / "status.json"
    write_status_atomic(
        target, {"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": -float("inf")}}
    )
    assert strict_loads(target.read_text(encoding="utf-8")) == {
        "a": None, "b": [None, 1.5], "c": {"d": None},
    }
    original = {"x": [1, {"y": 2.0}]}
    assert strict_loads(json.dumps(strict_json_safe(copy.deepcopy(original)))) == original
