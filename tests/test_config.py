import copy
from pathlib import Path

import pytest

from thermalctl.config import ConfigError, load_config, parse_config

GOOD = {
    "mode": "dry_run",
    "zones": [
        {
            "id": "cpu",
            "temperature_input": "t",
            "load_input": "l",
            "temperature_curve": [[40, 20], [80, 100]],
            "load_curve": [[0, 0], [90, 100]],
            "hard_max_temp_c": 90,
            "stale_after_s": 10,
        }
    ],
    "headers": [
        {
            "id": "h1",
            "path": "p",
            "mapped": False,
            "min_duty": 20,
            "min_rpm": 300,
            "stall_window_s": 15,
            "zones": ["cpu"],
        }
    ],
}

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "example.toml"


def mutated(fn):
    data = copy.deepcopy(GOOD)
    fn(data)
    return data


def test_good_config_parses():
    cfg = parse_config(copy.deepcopy(GOOD))
    assert cfg.mode == "dry_run"
    assert cfg.zones[0].temperature_curve[0] == (40.0, 20.0)
    assert cfg.headers[0].zones == ("cpu",)


def test_min_rpm_duty_defaults_and_parses():
    assert parse_config(copy.deepcopy(GOOD)).headers[0].min_rpm_duty == 50.0
    data = mutated(lambda d: d["headers"][0].update(min_rpm_duty=70))
    assert parse_config(data).headers[0].min_rpm_duty == 70.0


def test_example_validates():
    cfg = load_config(EXAMPLE)
    assert cfg.mode == "dry_run"
    assert all(not h.mapped for h in cfg.headers)


def test_mode_defaults_to_dry_run():
    data = mutated(lambda d: d.pop("mode"))
    assert parse_config(data).mode == "dry_run"


def test_active_mode_accepted():
    assert parse_config(mutated(lambda d: d.update(mode="active"))).mode == "active"


def test_optional_load_omitted():
    def f(d):
        del d["zones"][0]["load_input"], d["zones"][0]["load_curve"]

    zone = parse_config(mutated(f)).zones[0]
    assert zone.load_curve is None and zone.load_input is None


def zone_update(**kw):
    return lambda d: d["zones"][0].update(**kw)


def header_update(**kw):
    return lambda d: d["headers"][0].update(**kw)


CASES = {
    "bad mode": lambda d: d.update(mode="turbo"),
    "no zones": lambda d: d.update(zones=[]),
    "no headers": lambda d: d.update(headers=[]),
    "unsorted temp points": zone_update(temperature_curve=[[80, 100], [40, 20]]),
    "duplicate temp input": zone_update(temperature_curve=[[40, 20], [40, 30]]),
    "temp out of range": zone_update(temperature_curve=[[40, 20], [500, 100]]),
    "load out of range": zone_update(load_curve=[[0, 0], [150, 100]]),
    "unsorted load points": zone_update(load_curve=[[50, 10], [10, 20]]),
    "duty above 100": zone_update(temperature_curve=[[40, 20], [80, 101]]),
    "duty below 0": zone_update(temperature_curve=[[40, -1], [80, 100]]),
    "one point only": zone_update(temperature_curve=[[40, 20]]),
    "point not a pair": zone_update(temperature_curve=[[40, 20], [80]]),
    "point not a number": zone_update(temperature_curve=[[40, 20], [80, "x"]]),
    "load input without curve": lambda d: d["zones"][0].pop("load_curve"),
    "load curve without input": lambda d: d["zones"][0].pop("load_input"),
    "missing zone id": lambda d: d["zones"][0].pop("id"),
    "duplicate zone id": lambda d: d["zones"].append(copy.deepcopy(d["zones"][0])),
    "missing hard max": lambda d: d["zones"][0].pop("hard_max_temp_c"),
    "hard max out of range": zone_update(hard_max_temp_c=999),
    "staleness zero": zone_update(stale_after_s=0),
    "missing temperature input": lambda d: d["zones"][0].pop("temperature_input"),
    "unknown zone reference": header_update(zones=["nope"]),
    "empty zone list": header_update(zones=[]),
    "min duty above 100": header_update(min_duty=101),
    "min duty below 0": header_update(min_duty=-5),
    "negative min rpm": header_update(min_rpm=-1),
    "fractional min rpm": header_update(min_rpm=1.5),
    "min rpm duty above 100": header_update(min_rpm_duty=101),
    "stall window zero": header_update(stall_window_s=0),
    "mapped not bool": header_update(mapped="yes"),
    "missing header path": lambda d: d["headers"][0].pop("path"),
    "duplicate header id": lambda d: d["headers"].append(copy.deepcopy(d["headers"][0])),
}


@pytest.mark.parametrize("name", list(CASES))
def test_validation_errors(name):
    with pytest.raises(ConfigError):
        parse_config(mutated(CASES[name]))


def test_invalid_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("mode = [", newline="\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")
