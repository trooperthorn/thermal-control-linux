import json

import pytest

from thermalctl.backends.sysfs import (
    BackendError,
    SysfsBackend,
    duty_to_pwm,
    restore_from_state_file,
)


def put(path, text):
    path.write_text(text, encoding="ascii", newline="\n")


@pytest.fixture
def tree(tmp_path):
    d = tmp_path / "hw"
    d.mkdir()
    for n, enable in ((1, "5"), (2, "2"), (3, "5")):
        put(d / f"pwm{n}", "128\n")
        put(d / f"pwm{n}_enable", f"{enable}\n")
        put(d / f"fan{n}_input", "900\n")
    put(d / "temp1_input", "45500\n")
    return d


def make(tree, tmp_path, active=True, mapped=("p1", "p2")):
    return SysfsBackend(
        {"p1": str(tree / "pwm1"), "p2": str(tree / "pwm2"), "p3": str(tree / "pwm3")},
        {"cpu": str(tree / "temp1_input"), "gone": str(tree / "temp9_input")},
        mapped,
        tmp_path / "state.json",
        active=active,
        clock=lambda: 42.0,
    )


def text(p):
    return p.read_text().strip()


def break_file(path):
    """Replace a file with a directory so opening it for write fails."""
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize(
    "duty,pwm",
    [(0, 0), (100, 255), (50, 128), (-10, 0), (250, 255), (float("nan"), 255)],
)
def test_scaling_and_clamping(duty, pwm):
    assert duty_to_pwm(duty) == pwm


def test_reads(tree, tmp_path):
    b = make(tree, tmp_path)
    inputs = b.read_inputs()
    assert inputs["cpu"].value == 45.5 and inputs["cpu"].timestamp == 42.0
    assert b.read_rpm("p1") == 900.0


def test_read_errors_surface_as_missing(tree, tmp_path):
    b = make(tree, tmp_path)
    assert b.read_inputs()["gone"].value is None
    put(tree / "temp1_input", "garbage\n")
    assert b.read_inputs()["cpu"].value is None
    (tree / "fan1_input").unlink()
    assert b.read_rpm("p1") is None
    assert b.read_rpm("unknown") is None


def test_normal_exit_restores(tree, tmp_path):
    with make(tree, tmp_path) as b:
        assert text(tree / "pwm1_enable") == "1"
        assert text(tree / "pwm2_enable") == "1"
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["originals"] == {"p1": 5, "p2": 2}
        b.write_duty("p1", 100)
        assert text(tree / "pwm1") == "255"
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"
    assert not (tmp_path / "state.json").exists()


def test_exception_restores(tree, tmp_path):
    with pytest.raises(RuntimeError):
        with make(tree, tmp_path):
            raise RuntimeError("boom")
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"


def test_unmapped_and_dry_run_never_written(tree, tmp_path):
    with make(tree, tmp_path) as b:
        b.write_duty("p3", 100)
        b.release("p3")
        b.write_duty("nope", 100)
    assert text(tree / "pwm3") == "128" and text(tree / "pwm3_enable") == "5"
    with make(tree, tmp_path, active=False) as b:
        b.write_duty("p1", 100)
    assert text(tree / "pwm1") == "128" and text(tree / "pwm1_enable") == "5"
    assert not (tmp_path / "state.json").exists()


def test_failed_restore_writes_full_speed(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()
    break_file(tree / "pwm1_enable")
    b.write_duty("p1", 10)
    b.restore()
    assert text(tree / "pwm1") == "255"
    assert b.restore_failures == ["p1"]
    assert text(tree / "pwm2_enable") == "2"
    assert (tmp_path / "state.json").exists()


def test_release_falls_back_to_full_speed(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()
    break_file(tree / "pwm2_enable")
    b.release("p2")
    assert text(tree / "pwm2") == "255"


def test_start_failure_restores_and_raises(tree, tmp_path):
    (tree / "pwm2_enable").unlink()
    b = make(tree, tmp_path)
    with pytest.raises(BackendError):
        b.start()
    assert text(tree / "pwm1_enable") == "5"
    assert not (tmp_path / "state.json").exists()


def test_state_file_helper_after_kill(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()  # simulate SIGKILL: no restore runs
    assert text(tree / "pwm1_enable") == "1"
    assert restore_from_state_file(tmp_path / "state.json") == []
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"
    assert not (tmp_path / "state.json").exists()
    assert restore_from_state_file(tmp_path / "missing.json") == []
