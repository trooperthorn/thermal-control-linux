import json
import os
import signal

import pytest

from thermalctl.backends.sysfs import (
    BackendError,
    StateFileError,
    SysfsBackend,
    install_signal_handlers,
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
    [(0, 0), (100, 255), (50, 128), (-10, 0), (250, 255), (float("nan"), 255), (float("inf"), 255), (float("-inf"), 255)],
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
    put(tree / "pwm1", "128\n")  # the active run above left mapped p1 at full speed
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


def test_stale_state_file_restored_before_recording(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()  # simulate a killed run: manual mode set, state file left behind
    assert text(tree / "pwm1_enable") == "1"
    b2 = make(tree, tmp_path)
    b2.start()
    assert b2.originals == {"p1": 5, "p2": 2}
    b2.restore()
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("content", ["{not json", '{"headers": {"p1"', "[]", ""])
def test_corrupt_state_file_restores_to_firmware_and_starts(tree, tmp_path, content):
    """A bad state file must never leave fans in manual mode or stop the service starting."""
    state = tmp_path / "state.json"
    first = make(tree, tmp_path)
    first.start()  # a killed run: manual mode set at the old duty
    first.write_duty("p1", 20.0)
    state.write_text(content, encoding="utf-8")  # the kill truncated the state file
    assert text(tree / "pwm1_enable") == "1"
    b = make(tree, tmp_path)
    b.start()
    # Both mapped headers went to firmware mode first, so none kept a stale low duty.
    assert text(tree / "pwm1") == "255"
    assert text(tree / "pwm2") == "255"
    assert (tmp_path / "state.json.bad").read_text(encoding="utf-8") == content
    # The service then took control normally and recorded fresh originals.
    assert b.originals == {"p1": 5, "p2": 5}
    assert json.loads(state.read_text(encoding="utf-8"))["originals"] == {"p1": 5, "p2": 5}
    b.restore()
    assert text(tree / "pwm1_enable") == "5"
    assert not state.exists()


def test_stale_restore_failure_blocks_start(tree, tmp_path):
    make(tree, tmp_path).start()
    break_file(tree / "pwm1_enable")
    with pytest.raises(BackendError):
        make(tree, tmp_path).start()
    assert (tmp_path / "state.json").exists()


def test_restore_from_state_file_errors(tmp_path):
    assert restore_from_state_file(tmp_path / "none.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(StateFileError):
        restore_from_state_file(bad)


def test_state_file_with_non_pwm_path_is_refused(tmp_path):
    target = tmp_path / "victim"
    target.write_text("x", encoding="utf-8")
    bad = tmp_path / "evil.json"
    bad.write_text(
        json.dumps({"headers": {"p1": str(target)}, "originals": {"p1": 5}}),
        encoding="utf-8",
    )
    with pytest.raises(StateFileError):
        restore_from_state_file(bad)
    assert target.read_text(encoding="utf-8") == "x"


def test_release_full_speed_failure_is_logged(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()
    break_file(tree / "pwm1_enable")
    break_file(tree / "pwm1")
    b.release("p1")  # must not raise
    b.restore()


@pytest.fixture
def keep_handlers():
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in old.items():
        signal.signal(s, h)


def test_signal_triggers_restore(tree, tmp_path, keep_handlers):
    install_signal_handlers()
    b = make(tree, tmp_path)
    with pytest.raises(SystemExit):
        with b:
            assert text(tree / "pwm1_enable") == "1"
            signal.raise_signal(signal.SIGINT)
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"


def test_signal_during_restore_does_not_stop_it(tree, tmp_path, keep_handlers, monkeypatch):
    import thermalctl.backends.sysfs as mod

    install_signal_handlers()
    b = make(tree, tmp_path)
    b.start()
    real = mod._write_int
    fired = []

    def noisy(path, value):
        if not fired:
            fired.append(1)
            signal.raise_signal(signal.SIGINT)
        real(path, value)

    monkeypatch.setattr(mod, "_write_int", noisy)
    b.restore()
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm2_enable") == "2"
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN


def record_writes(monkeypatch):
    from thermalctl.backends import sysfs

    real = sysfs._write_int
    calls = []

    def spy(path, value):
        calls.append((os.path.basename(str(path)), value))
        return real(path, value)

    monkeypatch.setattr(sysfs, "_write_int", spy)
    return calls


def test_restore_manual_original_leaves_full_speed(tree, tmp_path):
    put(tree / "pwm1_enable", "1\n")
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    assert text(tree / "pwm1") != "255"
    b.restore()
    assert text(tree / "pwm1") == "255"
    assert text(tree / "pwm1_enable") == "1"


def test_restore_writes_full_speed_before_enable(tree, tmp_path, monkeypatch):
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    calls = record_writes(monkeypatch)
    b.restore()
    assert calls.index(("pwm1", 255)) < calls.index(("pwm1_enable", 5))
    assert text(tree / "pwm1_enable") == "5"
    assert text(tree / "pwm1") == "255"


def test_restore_from_state_file_leaves_full_speed(tree, tmp_path):
    put(tree / "pwm1_enable", "1\n")
    b = make(tree, tmp_path)
    b.start()  # simulate SIGKILL: no restore runs
    b.write_duty("p1", 10)
    assert restore_from_state_file(tmp_path / "state.json") == []
    assert text(tree / "pwm1") == "255"
    assert text(tree / "pwm1_enable") == "1"


def test_manual_original_without_full_speed_write_is_a_restore_failure(tree, tmp_path):
    """A manual original keeps the last duty, so a failed full speed write must not
    count as a successful restore or remove the state file."""
    put(tree / "pwm1_enable", "1\n")
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    break_file(tree / "pwm1")
    b.restore()
    assert text(tree / "pwm1_enable") == "1"
    assert b.restore_failures == ["p1"]
    assert (tmp_path / "state.json").exists()


def test_firmware_original_without_full_speed_write_hands_back_to_firmware(tree, tmp_path):
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    break_file(tree / "pwm1")
    b.restore()
    assert text(tree / "pwm1_enable") == "5"
    assert b.restore_failures == []


def test_restore_skips_full_speed_write_for_header_already_in_firmware_mode(tree, tmp_path, caplog):
    """After the mapping test releases a header to firmware mode 5, the chip rejects pwm
    writes with EBUSY. Restore must not try, and must not log a false error."""
    b = make(tree, tmp_path)
    b.start()
    b.write_duty("p1", 10)
    b.release("p1")  # back to firmware mode 5, as the mapping test does
    assert text(tree / "pwm1_enable") == "5"
    break_file(tree / "pwm1")  # stands in for the chip refusing the write
    with caplog.at_level("ERROR"):
        b.restore()
    assert b.restore_failures == []
    assert "full speed write to p1 failed" not in caplog.text
    assert text(tree / "pwm1_enable") == "5"
