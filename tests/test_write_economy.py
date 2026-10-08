"""Fan registers are written only when a value changes, and checked with reads otherwise.

Before this change every active header was written every cycle (30 writes a minute per
header at the 2 second default) and failsafe wrote manual mode and 255 every cycle. Now a
steady duty is written once, verified by one read per cycle, and written again when the
read disagrees or after FORCE_REFRESH_CYCLES.
"""

import thermalctl.backends.sysfs as sysfs_module
from thermalctl.backend import FakeBackend
from thermalctl.backends.sysfs import SysfsBackend
from thermalctl.config import parse_config
from thermalctl.controller import FORCE_REFRESH_CYCLES, Controller
from thermalctl.safety import FAILSAFE, Reading

ZONE = {
    "id": "cpu",
    "temperature_input": "temp",
    "temperature_curve": [[40, 20], [80, 100]],
    "hard_max_temp_c": 90,
    "stale_after_s": 10,
}
INTERVAL_S = 2.0


def header_cfg(hid):
    return {
        "id": hid, "path": "/fake/" + hid, "mapped": True, "min_duty": 20,
        "min_rpm": 300, "stall_window_s": 15, "zones": ["cpu"],
    }


def build(tmp_path, backend, headers=("pwm1", "pwm2"), **kwargs):
    config = parse_config(
        {"mode": "active", "zones": [ZONE], "headers": [header_cfg(h) for h in headers]}
    )
    now = {"t": 1000.0}
    ctl = Controller(
        config, backend, status_path=tmp_path / "status.json", clock=lambda: now["t"],
        hold_s=5.0, ema_alpha=1.0, hysteresis=0.0, ramp_down_per_s=1000.0, **kwargs,
    )

    def cycle(temp=60.0):
        now["t"] += INTERVAL_S
        backend.inputs = {"temp": Reading(temp, now["t"])}
        backend.rpms = {h: 900.0 for h in headers}
        return ctl.cycle()

    return ctl, cycle


def writes_to(backend, hid):
    return [w for w in backend.writes if w[0] == hid]


def test_steady_state_writes_are_a_tenth_of_one_per_cycle(tmp_path):
    """Measured on the fake: 600 cycles (20 minutes) of a constant 60 C, two headers.

    Before: 600 writes per header, 30 a minute. After: 1 + 600 // 60 = 11 at most.
    """
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend)
    cycles = 600
    for _ in range(cycles):
        cycle()
    for hid in ("pwm1", "pwm2"):
        count = len(writes_to(backend, hid))
        per_minute = count / (cycles * INTERVAL_S / 60.0)
        # The first write plus a forced refresh every FORCE_REFRESH_CYCLES, exactly.
        assert count == 1 + (cycles - 1) // FORCE_REFRESH_CYCLES == 10
        assert per_minute == 0.5  # against 30 a minute before, a sixtieth
    assert backend.retakes == []


def test_steady_state_writes_on_a_real_sysfs_tree(tmp_path, monkeypatch):
    tree = tmp_path / "hw"
    tree.mkdir()
    for n in (1, 2):
        (tree / f"pwm{n}").write_text("128\n", encoding="ascii")
        (tree / f"pwm{n}_enable").write_text("5\n", encoding="ascii")
        (tree / f"fan{n}_input").write_text("900\n", encoding="ascii")
    sysfs = SysfsBackend(
        {"pwm1": str(tree / "pwm1"), "pwm2": str(tree / "pwm2")}, {}, ["pwm1", "pwm2"],
        tmp_path / "state.json", active=True,
    )
    sysfs.start()
    real = sysfs_module._write_int
    pwm_writes = []
    enable_writes = []

    def spy(path, value):
        (enable_writes if path.endswith("_enable") else pwm_writes).append((path, value))
        return real(path, value)

    monkeypatch.setattr(sysfs_module, "_write_int", spy)

    class Wrapped(FakeBackend):
        """Real sysfs writes and reads, fake sensor inputs."""

        def write_duty(self, header_id, duty):
            sysfs.write_duty(header_id, duty)

        def holds(self, header_id, duty):
            return sysfs.holds(header_id, duty)

        def owns(self, header_id):
            return sysfs.owns(header_id)

        def retake(self, header_id):
            sysfs.retake(header_id)

        def release(self, header_id):
            sysfs.release(header_id)

        def released(self, header_id):
            return sysfs.released(header_id)

    ctl, cycle = build(tmp_path, Wrapped())
    cycles = 310  # past the forced refresh at cycle 301, so the next one is far off
    for _ in range(cycles):
        cycle()
    # Six writes per header: the first and the refreshes at cycles 61, 121, 181, 241, 301.
    assert len(pwm_writes) == 12
    assert enable_writes == []
    # An external pwm change is put back within one cycle.
    (tree / "pwm1").write_text("3\n", encoding="ascii")
    before = len(pwm_writes)
    cycle()
    assert len(pwm_writes) == before + 1
    assert int((tree / "pwm1").read_text()) == pwm_writes[-1][1]
    assert int((tree / "pwm1").read_text()) != 3
    ctl.shutdown()
    sysfs.restore()


def test_external_pwm_value_change_is_corrected_within_one_cycle(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    for _ in range(5):
        cycle()
    count = len(writes_to(backend, "pwm1"))
    expected = backend.pwm["pwm1"]
    backend.pwm["pwm1"] = 0
    doc = cycle()
    assert len(writes_to(backend, "pwm1")) == count + 1
    assert backend.pwm["pwm1"] == expected
    assert doc["headers"]["pwm1"]["state"] == "active"


def test_external_mode_change_is_corrected_within_one_cycle(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    for _ in range(5):
        cycle()
    backend.foreign.add("pwm1")
    doc = cycle()
    h = doc["headers"]["pwm1"]
    assert h["state"] == FAILSAFE
    assert "external_change:pwm1" in h["reasons"]
    assert backend.retakes == ["pwm1"]
    assert backend.writes[-1] == ("pwm1", 100.0)
    assert backend.pwm["pwm1"] == 255


def test_failsafe_holds_full_speed_with_one_write_and_verification_reads(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    for _ in range(3):
        cycle()
    backend.writes.clear()
    backend.holds_calls.clear()
    doc = cycle(120.0)  # implausible temperature
    assert doc["headers"]["pwm1"]["state"] == FAILSAFE
    for _ in range(19):
        doc = cycle(120.0)
        assert doc["headers"]["pwm1"]["duty"] == 100.0
    assert backend.writes == [("pwm1", 100.0)]
    assert backend.retakes == ["pwm1"]
    assert len(backend.holds_calls) == 19
    assert backend.releases == []


def test_failsafe_rewrites_when_the_value_or_mode_changes(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    cycle()
    cycle(120.0)
    cycle(120.0)
    assert backend.writes[-1] == ("pwm1", 100.0) and len(backend.retakes) == 1
    writes = len(backend.writes)
    backend.pwm["pwm1"] = 10  # another program lowers the duty
    cycle(120.0)
    assert len(backend.writes) == writes + 1 and backend.pwm["pwm1"] == 255
    backend.foreign.add("pwm1")  # another program changes the mode
    cycle(120.0)
    assert len(backend.retakes) == 3 and len(backend.writes) == writes + 2
    assert "pwm1" not in backend.foreign
    cycle(120.0)
    assert len(backend.writes) == writes + 2


def test_failsafe_to_firmware_releases_once_and_verifies_ownership(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",), failsafe_firmware=True)
    cycle()
    for _ in range(10):
        cycle(120.0)
    assert backend.releases == ["pwm1"]
    backend.foreign.add("pwm1")
    cycle(120.0)
    assert backend.releases == ["pwm1", "pwm1"]


def test_failed_failsafe_write_is_still_retried_every_cycle(tmp_path):
    class Failing(FakeBackend):
        def write_duty(self, header_id, duty):
            if duty == 100.0:
                raise OSError(5, "Input/output error")
            super().write_duty(header_id, duty)

    backend = Failing()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    cycle()
    for _ in range(4):
        cycle(120.0)
    assert backend.releases == ["pwm1"] * 4


def test_forced_refresh_rewrites_an_unchanged_value(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",), refresh_cycles=5)
    for _ in range(11):
        cycle()
    # Writes at cycle 1, 6 and 11 even though the verification read always agreed.
    assert len(writes_to(backend, "pwm1")) == 3
    assert FORCE_REFRESH_CYCLES == 60


def test_leaving_failsafe_takes_the_header_back_and_writes_again(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",))
    cycle()
    cycle(120.0)
    for _ in range(8):
        cycle(60.0)
    assert ctl.safety["pwm1"].state != FAILSAFE
    assert len(backend.retakes) == 2  # entering failsafe, then leaving it
    assert backend.pwm["pwm1"] == sysfs_module.duty_to_pwm(backend.writes[-1][1])
    assert backend.writes[-1][1] < 100.0


def test_dry_run_still_neither_writes_nor_reads_back(tmp_path):
    backend = FakeBackend()
    config = parse_config(
        {"mode": "dry_run", "zones": [ZONE], "headers": [header_cfg("pwm1")]}
    )
    now = {"t": 1000.0}
    ctl = Controller(config, backend, status_path=tmp_path / "s.json", clock=lambda: now["t"])
    for _ in range(3):
        now["t"] += 2.0
        backend.inputs = {"temp": Reading(60.0, now["t"])}
        backend.rpms = {"pwm1": 900.0}
        ctl.cycle()
    assert backend.writes == [] and backend.holds_calls == []


def test_sysfs_holds_reports_value_drift_and_unreadable_files(tmp_path):
    tree = tmp_path / "hw"
    tree.mkdir()
    (tree / "pwm1").write_text("128\n", encoding="ascii")
    (tree / "pwm1_enable").write_text("5\n", encoding="ascii")
    (tree / "pwm2").write_text("0\n", encoding="ascii")
    sysfs = SysfsBackend(
        {"pwm1": str(tree / "pwm1"), "pwm2": str(tree / "pwm2")}, {}, ["pwm1"],
        tmp_path / "state.json", active=True,
    )
    sysfs.start()
    sysfs.write_duty("pwm1", 50.0)
    assert sysfs.holds("pwm1", 50.0)
    assert not sysfs.holds("pwm1", 60.0)
    (tree / "pwm1").write_text("junk\n", encoding="ascii")
    assert not sysfs.holds("pwm1", 50.0)
    (tree / "pwm1").unlink()
    assert not sysfs.holds("pwm1", 50.0)
    assert sysfs.holds("pwm2", 100.0)  # not controlled: nothing to check
    (tree / "pwm1").write_text("0\n", encoding="ascii")
    sysfs.restore()


def _sysfs_tree(tmp_path, original_mode):
    tree = tmp_path / "hw"
    tree.mkdir()
    (tree / "pwm1").write_text("128\n", encoding="ascii")
    (tree / "pwm1_enable").write_text(f"{original_mode}\n", encoding="ascii")
    sysfs = SysfsBackend(
        {"pwm1": str(tree / "pwm1")}, {}, ["pwm1"], tmp_path / "state.json", active=True,
    )
    sysfs.start()
    return tree, sysfs


class _RealRelease(FakeBackend):
    """Fake inputs over a real sysfs backend, for the firmware failsafe paths."""

    def __init__(self, sysfs):
        super().__init__()
        self.sysfs = sysfs

    def write_duty(self, header_id, duty):
        self.sysfs.write_duty(header_id, duty)

    def holds(self, header_id, duty):
        return self.sysfs.holds(header_id, duty)

    def owns(self, header_id):
        return self.sysfs.owns(header_id)

    def retake(self, header_id):
        self.sysfs.retake(header_id)

    def release(self, header_id):
        self.releases.append(header_id)
        self.sysfs.release(header_id)

    def released(self, header_id):
        return self.sysfs.released(header_id)


def test_firmware_failsafe_into_manual_original_corrects_a_lowered_value(tmp_path):
    tree, sysfs = _sysfs_tree(tmp_path, 1)  # the original mode is manual
    backend = _RealRelease(sysfs)
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",), failsafe_firmware=True)
    cycle()
    cycle(120.0)
    assert (tree / "pwm1").read_text().strip() == "255"
    assert backend.releases == ["pwm1"]
    cycle(120.0)
    assert backend.releases == ["pwm1"]  # verified by reads, no rewrite
    (tree / "pwm1").write_text("10\n", encoding="ascii")  # another program lowers it
    cycle(120.0)
    assert backend.releases == ["pwm1", "pwm1"]
    assert (tree / "pwm1").read_text().strip() == "255"


def test_firmware_failsafe_retries_a_release_whose_mode_write_failed(tmp_path, monkeypatch):
    tree, sysfs = _sysfs_tree(tmp_path, 5)
    backend = _RealRelease(sysfs)
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",), failsafe_firmware=True)
    cycle()
    real = sysfs_module._write_int
    failing = {"on": True}

    def flaky(path, value):
        if failing["on"] and path.endswith("_enable"):
            raise OSError(5, "Input/output error")
        return real(path, value)

    monkeypatch.setattr(sysfs_module, "_write_int", flaky)
    cycle(120.0)
    cycle(120.0)
    cycle(120.0)
    assert backend.releases == ["pwm1"] * 3  # retried every cycle while it keeps failing
    failing["on"] = False
    cycle(120.0)
    assert backend.releases == ["pwm1"] * 4
    assert (tree / "pwm1_enable").read_text().strip() == "5"
    cycle(120.0)
    assert backend.releases == ["pwm1"] * 4  # now verified, no further writes


def test_failsafe_release_that_did_not_take_effect_is_retried(tmp_path):
    backend = FakeBackend()
    ctl, cycle = build(tmp_path, backend, headers=("pwm1",), failsafe_firmware=True)
    cycle()
    backend.unreleased.add("pwm1")
    for _ in range(3):
        cycle(120.0)
    assert backend.releases == ["pwm1"] * 3
