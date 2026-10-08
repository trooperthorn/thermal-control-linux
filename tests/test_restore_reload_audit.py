"""Restore order, reloads that add headers, complete audit lines and absurd expiry values."""

import logging

import pytest
from test_override_install import host, run_install  # noqa: F401
from test_overrides_reload import trusted  # noqa: F401
from test_reload_safety import MAIN, Rig
from test_sysfs import make, put, record_writes, text, tree  # noqa: F401

from thermalctl.config import ConfigError, load_config, parse_config
from thermalctl.controller import describe_changes


def test_restore_of_a_manual_original_writes_full_speed_first_from_mode_5(tree, tmp_path, monkeypatch):  # noqa: F811
    put(tree / "pwm1_enable", "1\n")
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    # Another program hands the header to the chip: it is in mode 5 when restore runs.
    put(tree / "pwm1_enable", "5\n")
    calls = record_writes(monkeypatch)
    b.restore()
    assert calls.index(("pwm1", 255)) < calls.index(("pwm1_enable", 1))
    assert text(tree / "pwm1") == "255"
    assert text(tree / "pwm1_enable") == "1"


def test_restore_of_a_firmware_original_does_not_write_full_speed_from_firmware_mode(
    tree, tmp_path, monkeypatch  # noqa: F811
):
    b = make(tree, tmp_path)
    b.start()
    put(tree / "pwm1_enable", "5\n")
    calls = record_writes(monkeypatch)
    b.restore()
    assert ("pwm1", 255) not in calls


EXTRA_HEADER = """
[[headers]]
id = "pwm2"
path = "/fake/pwm2"
mapped = false
min_duty = 40
min_rpm = 300
stall_window_s = 15
zones = ["cpu"]
"""


def test_reload_adding_an_unmapped_header_leaves_no_permanent_failsafe(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.write(rig.main, MAIN + EXTRA_HEADER)
    for _ in range(10):
        doc = rig.cycle()
    # The running backend never read this header's fan, so the header must not sit in
    # failsafe for want of an RPM reading. The reload is accepted.
    assert rig.ctl.config_error is None
    assert "pwm2" in {h.id for h in rig.ctl.config.headers}
    assert doc["headers"]["pwm2"]["state"] == "dry_run"
    assert doc["headers"]["pwm2"]["reasons"] == []
    assert doc["headers"]["pwm1"]["state"] == "active"


def test_a_header_known_at_start_can_be_unmapped_and_back_without_failsafe(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.write(rig.main, MAIN.replace("mapped = true", "mapped = false"))
    for _ in range(3):
        doc = rig.cycle()
    assert doc["headers"]["pwm1"]["state"] == "dry_run"


def test_changing_min_duty_limit_is_audited():
    base = load_config_from(MAIN)
    changed = load_config_from(MAIN.replace("min_duty_limit = 10", "min_duty_limit = 5"))
    assert any("min_duty_limit=10" in line and "->5" in line for line in describe_changes(base, changed))


def test_changing_the_plausible_range_is_audited():
    base = load_config_from(MAIN)
    changed = load_config_from(MAIN.replace("stale_after_s = 10", "stale_after_s = 10\nplausible_max_c = 120"))
    lines = describe_changes(base, changed)
    assert any("plausible_max_c" in line for line in lines)


def test_every_field_of_every_record_is_audited():
    import dataclasses

    from thermalctl.config import Header, Zone

    base = load_config_from(MAIN)
    for cls, attr in ((Header, "headers"), (Zone, "zones")):
        record = getattr(base, attr)[0]
        for f in dataclasses.fields(cls):
            if f.name == "id":
                continue
            value = getattr(record, f.name)
            other = (value + 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            if other is None:
                continue
            changed_record = dataclasses.replace(record, **{f.name: other})
            changed = dataclasses.replace(base, **{attr: (changed_record,)})
            assert any(f.name in line for line in describe_changes(base, changed)), f.name


def load_config_from(text_):
    import tomllib

    return parse_config(tomllib.loads(text_))


def test_last_change_survives_a_reload(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.backend.inputs = {}  # a missing input sends the header to failsafe
    rig.now += 1.0
    rig.ctl.cycle()
    machine = rig.ctl.safety["pwm1"]
    assert machine.state == "failsafe"
    stamp = machine.last_change
    assert stamp is not None
    rig.write(rig.main, MAIN.replace("min_duty = 40", "min_duty = 45"))
    rig.cycle()
    assert rig.ctl.safety["pwm1"].last_change == stamp


def test_last_change_of_a_healthy_header_survives_a_reload(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    machine = rig.ctl.safety["pwm1"]
    machine.last_change = 777.0
    rig.write(rig.main, MAIN.replace("min_duty = 40", "min_duty = 45"))
    rig.cycle()
    assert rig.ctl.safety["pwm1"].last_change == 777.0


HUGE = "9" * 5000


def test_a_5000_digit_expires_at_is_a_clean_validation_error(tmp_path, trusted):  # noqa: F811
    from thermalctl.config import apply_overrides

    cfg = tmp_path / "config.toml"
    cfg.write_text(MAIN, encoding="ascii", newline="\n")
    over = tmp_path / "overrides.toml"
    over.write_text(f"expires_at = {HUGE}\n[headers.pwm1]\nmin_duty = 25\n", encoding="ascii", newline="\n")
    with pytest.raises(ConfigError):
        apply_overrides(load_config(cfg), over)


def test_install_with_a_5000_digit_expires_at_exits_non_zero_without_a_traceback(
    host, capsys, monkeypatch  # noqa: F811
):
    before = host.live.read_bytes()
    code, out = run_install(host, f"expires_at = {HUGE}\n[headers.pwm1]\nmin_duty = 25\n", capsys, monkeypatch)
    assert code == 1
    assert "Traceback" not in out.err
    assert host.live.read_bytes() == before


@pytest.mark.parametrize("number", [10**400, 10**30, -(10**400), 0, -1])
def test_other_absurd_integer_expiries_are_clean_errors(number):
    from thermalctl.config import _expires_at

    with pytest.raises(ConfigError):
        _expires_at(number)
