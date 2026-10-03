import pytest

from thermalctl.curves import interpolate, zone_duty

TEMP = [(40.0, 20.0), (60.0, 40.0), (80.0, 100.0)]
LOAD = [(0.0, 0.0), (50.0, 30.0), (90.0, 100.0)]


def test_at_points():
    assert interpolate(TEMP, 40) == 20
    assert interpolate(TEMP, 60) == 40
    assert interpolate(TEMP, 80) == 100


def test_between_points():
    assert interpolate(TEMP, 50) == pytest.approx(30)
    assert interpolate(TEMP, 70) == pytest.approx(70)


def test_clamped_beyond_ends():
    assert interpolate(TEMP, -10) == 20
    assert interpolate(TEMP, 39.9) == 20
    assert interpolate(TEMP, 200) == 100


def test_empty_curve_rejected():
    with pytest.raises(ValueError):
        interpolate([], 1)


def test_load_raises_duty():
    assert zone_duty(TEMP, 40, LOAD, 90) == 100


def test_load_never_lowers_duty():
    assert zone_duty(TEMP, 80, LOAD, 0) == 100
    assert zone_duty(TEMP, 60, LOAD, 10) == 40


def test_no_load_curve_or_reading_uses_temperature():
    assert zone_duty(TEMP, 60) == 40
    assert zone_duty(TEMP, 60, LOAD, None) == 40
    assert zone_duty(TEMP, 60, None, 99) == 40
