import copy
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from thermalctl import config as config_module
from thermalctl.__main__ import main
from thermalctl.config import ConfigError, apply_overrides, load_effective, parse_config

GOOD = {
    "mode": "dry_run",
    "zones": [
        {
            "id": "cpu",
            "temperature_input": "t",
            "temperature_curve": [[40, 20], [80, 100]],
            "hard_max_temp_c": 90,
            "stale_after_s": 10,
        }
    ],
    "headers": [
        {
            "id": "h1",
            "path": "p",
            "mapped": True,
            "min_duty": 30,
            "min_rpm": 300,
            "stall_window_s": 15,
            "zones": ["cpu"],
        }
    ],
}


def put(path, text):
    Path(path).write_text(text, encoding="ascii", newline="\n")


def base(limit=10, mapped=True):
    data = copy.deepcopy(GOOD)
    first = data["headers"][0]
    first.update(id="pwm2", mapped=mapped, min_duty=30)
    if limit is not None:
        first["min_duty_limit"] = limit
    return parse_config(data)


@pytest.fixture
def trusted(monkeypatch):
    """Make the overrides file look root-owned and private on every platform."""
    monkeypatch.setattr(config_module, "_posix", lambda: True)
    monkeypatch.setattr(
        config_module,
        "_stat_file",
        lambda path: SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600),
    )


def overrides(tmp_path, text):
    path = tmp_path / "overrides.toml"
    put(path, text)
    return path


def test_min_duty_limit_defaults_to_min_duty_and_cannot_exceed_it():
    assert base(limit=None).headers[0].min_duty_limit == 30
    assert base(limit=10).headers[0].min_duty_limit == 10
    data = copy.deepcopy(GOOD)
    data["headers"][0]["min_duty_limit"] = 40
    with pytest.raises(ConfigError, match="min_duty_limit"):
        parse_config(data)
    data["headers"][0]["min_duty_limit"] = 101
    with pytest.raises(ConfigError, match="between 0 and 100"):
        parse_config(data)


def test_valid_override_lowers_pwm2_to_20(tmp_path, trusted):
    path = overrides(tmp_path, '[headers.pwm2]\nmin_duty = 20\n')
    merged, report = apply_overrides(base(), path)
    assert merged.headers[0].min_duty == 20
    assert report.applied and report.min_duty == {"pwm2": 20.0}
    assert merged.mode == "dry_run"


def test_missing_file_means_no_overrides(tmp_path):
    config = base()
    merged, report = apply_overrides(config, tmp_path / "none.toml")
    assert merged is config and not report.applied and report.ignored is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("[headers.pwm2]\nmin_duty = 5\n", "below the allowed minimum 10"),
        ("[headers.pwm2]\nmin_duty = 101\n", "between 0 and 100"),
        ("[headers.pwm2]\nmin_duty = 20\nmin_rpm = 0\n", "'min_rpm' is not allowed"),
        ("hard_max = 1\n", "'hard_max' is not allowed"),
        ("[headers.pwm9]\nmin_duty = 20\n", "pwm9 is not in the config"),
        ('mode = "turbo"\n', "mode must be one of"),
        ("[headers.pwm2]\nmin_duty = true\n", "must be a number"),
        ("mode = [\n", "not valid TOML"),
    ],
)
def test_bad_overrides_are_rejected(tmp_path, trusted, text, message):
    with pytest.raises(ConfigError, match=message):
        apply_overrides(base(), overrides(tmp_path, text))


def test_unmapped_header_floor_is_rejected(tmp_path, trusted):
    path = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 20\n")
    with pytest.raises(ConfigError, match="not mapped"):
        apply_overrides(base(mapped=False), path)


def test_mode_active_with_an_unmapped_header_is_rejected(tmp_path, trusted):
    path = overrides(tmp_path, 'mode = "active"\n')
    with pytest.raises(ConfigError, match="every header mapped"):
        apply_overrides(base(mapped=False), path)
    merged, report = apply_overrides(base(mapped=True), path)
    assert merged.mode == "active" and report.mode == "active"


@pytest.mark.parametrize(
    "uid, mode, reason",
    [(1000, 0o600, "not owned by root"), (0, 0o664, "group or by others"), (0, 0o602, "group or by others")],
)
def test_insecure_file_is_ignored(tmp_path, monkeypatch, uid, mode, reason):
    path = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 20\n")
    monkeypatch.setattr(config_module, "_posix", lambda: True)
    monkeypatch.setattr(
        config_module, "_stat_file",
        lambda p: SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode),
    )
    config = base()
    merged, report = apply_overrides(config, path)
    assert merged is config and not report.applied
    assert reason in report.ignored


def test_permissions_are_not_checked_off_posix(tmp_path, monkeypatch):
    path = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 20\n")
    monkeypatch.setattr(config_module, "_posix", lambda: False)
    merged, report = apply_overrides(base(), path)
    assert report.applied and merged.headers[0].min_duty == 20


def write_main(tmp_path, limit="10"):
    text = (
        'mode = "dry_run"\n[[zones]]\nid = "cpu"\ntemperature_input = "t"\n'
        "temperature_curve = [[40, 20], [80, 100]]\nhard_max_temp_c = 90\nstale_after_s = 10\n"
    )
    for n in (1, 2):
        text += f"""
[[headers]]
id = "pwm{n}"
path = "p{n}"
mapped = true
min_duty = 30
min_duty_limit = {limit}
min_rpm = 300
stall_window_s = 15
zones = ["cpu"]
"""
    path = tmp_path / "config.toml"
    put(path, text)
    return path


def test_load_effective_does_not_rewrite_the_main_config(tmp_path, trusted):
    main_path = write_main(tmp_path)
    before = main_path.read_bytes()
    over = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 20\n")
    config, report = load_effective(main_path, over)
    assert {h.id: h.min_duty for h in config.headers} == {"pwm1": 30, "pwm2": 20}
    assert main_path.read_bytes() == before


def test_check_config_shows_effective_values(tmp_path, capsys, trusted):
    main_path = write_main(tmp_path)
    over = overrides(tmp_path, '[headers.pwm2]\nmin_duty = 20\n')
    assert main(["check-config", str(main_path), "--overrides", str(over)]) == 0
    out = capsys.readouterr().out
    assert "overrides: applied" in out
    assert "effective pwm2: min_duty 20 (overridden)" in out
    assert "effective pwm1: min_duty 30\n" in out


def test_check_config_without_overrides(tmp_path, capsys):
    main_path = write_main(tmp_path)
    assert main(["check-config", str(main_path), "--overrides", str(tmp_path / "x.toml")]) == 0
    out = capsys.readouterr().out
    assert "overrides: none" in out and "effective pwm2: min_duty 30" in out


def test_check_config_rejects_bad_override(tmp_path, capsys, trusted):
    main_path = write_main(tmp_path)
    over = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 5\n")
    assert main(["check-config", str(main_path), "--overrides", str(over)]) == 1
    assert "below the allowed minimum" in capsys.readouterr().err


def test_check_config_exits_non_zero_when_overrides_are_ignored(tmp_path, capsys, monkeypatch):
    main_path = write_main(tmp_path)
    over = overrides(tmp_path, "[headers.pwm2]\nmin_duty = 20\n")
    monkeypatch.setattr(config_module, "_posix", lambda: True)
    monkeypatch.setattr(
        config_module, "_stat_file",
        lambda p: SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o600),
    )
    # Regression: an ignored overrides file used to exit 0, so a caller could not tell that
    # its override had no effect.
    assert main(["check-config", str(main_path), "--overrides", str(over)]) == 1
    captured = capsys.readouterr()
    assert "overrides: ignored" in captured.out
    assert "effective pwm2: min_duty 30" in captured.out
    assert "not owned by root" in captured.err
