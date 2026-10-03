import json

import pytest
import logging

from thermalctl.backend import FakeBackend
from thermalctl.config import parse_config
from thermalctl.controller import Controller
from thermalctl.safety import Reading

ZONE = {
    "id": "cpu",
    "temperature_input": "temp",
    "temperature_curve": [[40, 20], [80, 100]],
    "hard_max_temp_c": 90,
    "stale_after_s": 10,
    "load_input": "load",
    "load_curve": [[0, 0], [100, 100]],
}


def header(hid, mapped):
    return {
        "id": hid, "path": "/fake/" + hid, "mapped": mapped, "min_duty": 20,
        "min_rpm": 300, "stall_window_s": 15, "zones": ["cpu"],
    }


def make_config(mode="active"):
    return parse_config(
        {"mode": mode, "zones": [ZONE], "headers": [header("pwm1", True), header("pwm2", False)]}
    )


class Rig:
    def __init__(self, tmp_path, mode="active"):
        self.now = 1000.0
        self.backend = FakeBackend()
        self.status = tmp_path / "status.json"
        self.ctl = Controller(
            make_config(mode), self.backend, status_path=self.status,
            clock=lambda: self.now, hold_s=5.0, ema_alpha=1.0,
        )
        self.set_inputs(50.0, 10.0)
        self.backend.rpms = {"pwm1": 900.0, "pwm2": 900.0}

    def set_inputs(self, temp, load):
        self.backend.inputs = {
            "temp": Reading(temp, self.now), "load": Reading(load, self.now),
        }

    def cycle(self, advance=1.0):
        self.now += advance
        self.set_inputs(self.backend.inputs["temp"].value, self.backend.inputs["load"].value)
        return self.ctl.cycle()

    def doc(self):
        return json.loads(self.status.read_text(encoding="utf-8"))


def test_dry_run_never_writes(tmp_path):
    rig = Rig(tmp_path, "dry_run")
    for _ in range(3):
        rig.cycle()
    rig.backend.fail_reads = True
    rig.cycle()
    rig.ctl.shutdown()
    assert rig.backend.writes == [] and rig.backend.releases == []
    assert rig.doc()["mode"] == "dry_run"


def test_active_writes_only_mapped_headers(tmp_path):
    rig = Rig(tmp_path)
    doc = rig.cycle()
    assert [w[0] for w in rig.backend.writes] == ["pwm1"]
    assert doc["headers"]["pwm1"]["state"] == "active"
    assert doc["headers"]["pwm2"]["state"] == "dry_run"
    assert doc["headers"]["pwm2"]["duty"] is not None


def test_quiet_when_cool_and_full_when_hot(tmp_path):
    rig = Rig(tmp_path)
    rig.set_inputs(30.0, 0.0)
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 20.0
    rig.set_inputs(85.0, 95.0)
    assert rig.cycle()["headers"]["pwm1"]["duty"] == 100.0


def test_read_exception_fails_safe_then_recovers(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.backend.writes.clear()
    rig.backend.fail_reads = True
    doc = rig.cycle()
    for hid in ("pwm1", "pwm2"):
        assert doc["headers"][hid]["state"] == "failsafe"
        assert any(r.startswith("cycle_error") for r in doc["headers"][hid]["reasons"])
    assert rig.backend.writes == [("pwm1", 100.0)]
    rig.backend.fail_reads = False
    assert rig.cycle()["headers"]["pwm1"]["state"] == "failsafe"
    doc = rig.cycle(advance=6.0)
    doc = rig.cycle(advance=6.0)
    assert doc["headers"]["pwm1"]["state"] == "active"
    assert doc["headers"]["pwm2"]["state"] == "dry_run"


def test_status_file_valid_after_every_cycle_and_no_temp(tmp_path):
    rig = Rig(tmp_path)
    for i in range(5):
        rig.backend.fail_reads = i == 2
        rig.cycle()
        doc = rig.doc()
        assert {"version", "timestamp", "mode", "headers", "zones"} <= doc.keys()
        assert set(doc["headers"]["pwm1"]) >= {"state", "duty", "rpm", "reasons"}
        assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_failed_status_write_keeps_previous_file(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path)
    rig.cycle()
    before = rig.status.read_text(encoding="utf-8")

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("thermalctl.controller.os.replace", broken)
    rig.cycle()
    assert rig.status.read_text(encoding="utf-8") == before
    assert "status file write failed" in caplog.text


def test_invalid_reload_keeps_failsafe_and_logs(tmp_path, caplog):
    rig = Rig(tmp_path)
    rig.cycle()
    bad = tmp_path / "bad.toml"
    bad.write_text("mode = 'sideways'\n", encoding="utf-8", newline="\n")
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        assert rig.ctl.reload(bad) is False
    assert "config reload rejected" in caplog.text
    doc = rig.cycle()
    assert doc["config_valid"] is False
    assert doc["headers"]["pwm1"]["state"] == "failsafe"
    assert "invalid_config" in doc["headers"]["pwm1"]["reasons"]
    assert rig.backend.writes[-1] == ("pwm1", 100.0)


def test_valid_reload_audits_old_and_new(tmp_path, caplog):
    rig = Rig(tmp_path)
    good = tmp_path / "good.toml"
    good.write_text(
        'mode = "dry_run"\n[[zones]]\nid = "cpu"\ntemperature_input = "temp"\n'
        "temperature_curve = [[40, 20], [80, 100]]\nhard_max_temp_c = 90\nstale_after_s = 10\n"
        '[[headers]]\nid = "pwm1"\npath = "/fake/pwm1"\nmapped = true\nmin_duty = 20\n'
        'min_rpm = 300\nstall_window_s = 15\nzones = ["cpu"]\n',
        encoding="utf-8", newline="\n",
    )
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        assert rig.ctl.reload(good) is True
    assert "mode=active->dry_run" in caplog.text
    assert "header pwm2 present=True->False" in caplog.text


def test_shutdown_goes_full_speed_on_mapped_headers(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.ctl.shutdown()
    assert rig.backend.writes[-1] == ("pwm1", 100.0)
    assert rig.doc()["headers"]["pwm1"]["state"] == "failsafe"


def _reload_text(mode, mapped, with_pwm2=True):
    zone = (
        'mode = "' + mode + '"\n[[zones]]\nid = "cpu"\ntemperature_input = "temp"\n'
        "temperature_curve = [[40, 20], [80, 100]]\nhard_max_temp_c = 90\nstale_after_s = 10\n"
    )

    def hdr(hid, is_mapped):
        return (
            '[[headers]]\nid = "' + hid + '"\npath = "/fake/' + hid + '"\nmapped = ' + is_mapped
            + '\nmin_duty = 20\nmin_rpm = 300\nstall_window_s = 15\nzones = ["cpu"]\n'
        )

    return zone + hdr("pwm1", mapped) + (hdr("pwm2", "false") if with_pwm2 else "")


def _low_then_reload(tmp_path, text):
    rig = Rig(tmp_path)
    rig.set_inputs(30.0, 0.0)
    rig.cycle()
    assert rig.backend.writes[-1] == ("pwm1", 20.0)
    new = tmp_path / "new.toml"
    new.write_text(text, encoding="utf-8", newline="\n")
    assert rig.ctl.reload(new) is True
    return rig


def test_reload_to_dry_run_drives_header_to_full_speed(tmp_path):
    rig = _low_then_reload(tmp_path, _reload_text("dry_run", "true"))
    assert rig.backend.writes[-1] == ("pwm1", 100.0)
    count = len(rig.backend.writes)
    rig.cycle()
    assert len(rig.backend.writes) == count


def test_reload_unmapping_header_drives_it_to_full_speed(tmp_path):
    rig = _low_then_reload(tmp_path, _reload_text("active", "false"))
    assert rig.backend.writes[-1] == ("pwm1", 100.0)


def test_reload_removing_header_drives_it_to_full_speed(tmp_path):
    new_text = _reload_text("active", "false", with_pwm2=False).replace("pwm1", "pwm3")
    rig = _low_then_reload(tmp_path, new_text)
    assert rig.backend.writes[-1] == ("pwm1", 100.0)


def test_reload_keeping_header_active_does_not_force_full_speed(tmp_path):
    rig = _low_then_reload(tmp_path, _reload_text("active", "true"))
    assert rig.backend.writes[-1] == ("pwm1", 20.0)


def test_interrupted_json_dump_keeps_previous_file_and_no_temp(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.cycle()
    before = rig.status.read_text(encoding="utf-8")

    def partial(document, handle, **kwargs):
        handle.write('{"version": ')
        raise OSError("interrupted")

    monkeypatch.setattr("thermalctl.controller.json.dump", partial)
    rig.cycle()
    assert rig.status.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_failed_replace_leaves_no_temp_file(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.cycle()

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("thermalctl.controller.os.replace", broken)
    rig.cycle()
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_failsafe_on_one_header_keeps_other_header_smoothing(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    ema = rig.ctl.emas[("cpu", "temp")]
    ema.update(70.0)
    rig.ctl._failsafe_header(rig.ctl.config.headers[0])
    assert ema.value is None


def test_first_cycle_warms_up_load_then_second_cycle_uses_it(tmp_path):
    rig = Rig(tmp_path)
    rig.set_inputs(50.0, 100.0)
    warm = Reading(None, None, warming_up=True)

    def cycle(load_reading):
        rig.now += 1.0
        rig.backend.inputs = {"temp": Reading(50.0, rig.now), "load": load_reading}
        return rig.ctl.cycle()

    doc = cycle(warm)
    header = doc["headers"]["pwm1"]
    assert header["state"] == "active" and header["reasons"] == []
    assert header["notes"] == ["load_warming_up"]
    assert header["duty"] == pytest.approx(40.0)  # temperature curve alone at 50 C
    doc = cycle(Reading(100.0, rig.now))
    header = doc["headers"]["pwm1"]
    assert header["state"] == "active" and header["notes"] == []
    assert header["duty"] == 100.0  # load curve now wins


@pytest.mark.parametrize("temp", [0.0, -40.0, 200.0])
def test_implausible_temperature_forces_failsafe(tmp_path, temp):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.set_inputs(temp, 10.0)
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["state"] == "failsafe"
    assert "invalid_input:temp" in doc["headers"]["pwm1"]["reasons"]
    assert rig.backend.writes[-1] == ("pwm1", 100.0)


# -- reloads that need a restart ----------------------------------------------------


def _write_reload(tmp_path, text):
    new = tmp_path / "new.toml"
    new.write_text(text, encoding="utf-8", newline="\n")
    return new


def test_reload_from_dry_run_to_active_is_refused(tmp_path, caplog):
    rig = Rig(tmp_path, mode="dry_run")
    rig.cycle()
    with caplog.at_level(logging.ERROR, logger="thermalctl.audit"):
        assert rig.ctl.reload(_write_reload(tmp_path, _reload_text("active", "true"))) is False
    assert "mode dry_run->active" in caplog.text
    assert rig.ctl.config.mode == "dry_run"
    assert rig.ctl.config_valid is True
    rig.cycle()
    assert rig.backend.writes == []
    assert rig.doc()["headers"]["pwm1"]["state"] == "dry_run"


def test_reload_that_maps_another_header_is_refused(tmp_path):
    rig = Rig(tmp_path)
    text = _reload_text("active", "true").replace("false", "true")
    assert rig.ctl.reload(_write_reload(tmp_path, text)) is False
    assert [h.mapped for h in rig.ctl.config.headers] == [True, False]


def test_reload_that_moves_a_mapped_header_path_is_refused(tmp_path):
    rig = Rig(tmp_path)
    text = _reload_text("active", "true").replace("/fake/pwm1", "/fake/other1")
    assert rig.ctl.reload(_write_reload(tmp_path, text)) is False
    assert rig.ctl.config.headers[0].path == "/fake/pwm1"


def test_reload_applies_the_config_transform(tmp_path):
    rig = Rig(tmp_path)
    rig.ctl.config_transform = lambda c: c
    assert rig.ctl.reload(_write_reload(tmp_path, _reload_text("active", "true"))) is True


def test_failing_config_transform_rejects_the_reload(tmp_path):
    rig = Rig(tmp_path)

    def boom(config):
        raise ValueError("chip gone")

    rig.ctl.config_transform = boom
    assert rig.ctl.reload(_write_reload(tmp_path, _reload_text("active", "true"))) is False
    assert rig.ctl.config_valid is False


def test_external_change_puts_a_header_in_failsafe_and_recovers_after_the_hold(tmp_path):
    rig = Rig(tmp_path)
    rig.set_inputs(30.0, 0.0)
    rig.cycle()
    rig.backend.foreign = {"pwm1"}
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["state"] == "failsafe"
    assert doc["headers"]["pwm1"]["reasons"] == ["external_change:pwm1"]
    assert rig.backend.writes[-1] == ("pwm1", 100.0)
    rig.backend.foreign = set()
    for _ in range(10):
        doc = rig.cycle()
    assert doc["headers"]["pwm1"]["state"] == "active"
