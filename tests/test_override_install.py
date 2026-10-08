"""The install-override command, override expiry, and the packaged sudoers and unit files."""

import io
import json
import logging
import re
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_overrides_reload import MAIN, Rig, put, trusted  # noqa: F401

from thermalctl import config as config_module
from thermalctl.backend import FakeBackend
from thermalctl import install as install_module
from thermalctl.__main__ import main
from thermalctl.config import ConfigError, apply_overrides, load_config
from thermalctl.controller import Controller
from thermalctl.install import InstallError, install_override
from thermalctl.safety import Reading

ROOT = Path(__file__).resolve().parent.parent
LIVE = "[headers.pwm1]\nmin_duty = 25\n"


@pytest.fixture
def host(tmp_path, monkeypatch, trusted):  # noqa: F811
    """A main config, a live overrides file and a recording stand-in for chown."""
    put(tmp_path / "config.toml", MAIN)
    put(tmp_path / "overrides.toml", LIVE)
    chowned = []
    monkeypatch.setattr(install_module, "chown_root", chowned.append)
    return SimpleNamespace(
        config=tmp_path / "config.toml", live=tmp_path / "overrides.toml", dir=tmp_path,
        chowned=chowned,
    )


def run_install(host, text, capsys, monkeypatch, *extra):
    stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", stdin)
    code = main([
        "install-override", "--config", str(host.config), "--overrides", str(host.live),
        "--lock-file", str(host.dir / "none.lock"), *extra,
    ])
    return code, capsys.readouterr()


def leftovers(host):
    return sorted(p.name for p in host.dir.iterdir() if p.name.startswith(".overrides-"))


def test_install_accepts_a_valid_candidate_from_stdin(host, capsys, monkeypatch):
    code, out = run_install(host, "[headers.pwm1]\nmin_duty = 22.5\n", capsys, monkeypatch)
    assert code == 0, out.err
    assert host.live.read_text(encoding="ascii") == "[headers.pwm1]\nmin_duty = 22.5\n"
    assert "override installed" in out.out
    # The file was made root-owned before it was validated and moved into place.
    assert len(host.chowned) == 1
    assert leftovers(host) == []
    # The service reads the installed file as the float it was written as.
    config = apply_overrides(load_config(host.config), host.live)[0]
    assert config.headers[0].min_duty == 22.5


def test_install_sets_mode_0644_before_the_file_goes_live(host, monkeypatch):
    modes = []
    real = install_module.os.chmod
    monkeypatch.setattr(
        install_module.os, "chmod", lambda path, mode: (modes.append(mode), real(path, mode))
    )
    install_override(b"[headers.pwm1]\nmin_duty = 30\n", host.config, host.live)
    assert modes == [0o644]
    if install_module.os.name == "posix":
        assert stat.S_IMODE(host.live.stat().st_mode) & 0o022 == 0


def test_install_reads_a_fixed_path(host, capsys, monkeypatch):
    candidate = host.dir / "candidate.toml"
    put(candidate, "[headers.pwm1]\nmin_duty = 33\n")
    code, out = run_install(host, "", capsys, monkeypatch, "--from", str(candidate))
    assert code == 0, out.err
    assert "min_duty = 33" in host.live.read_text(encoding="ascii")


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("[headers.pwm1]\nmin_duty = 5\n", "below the allowed minimum"),
        ("[headers.pwm9]\nmin_duty = 20\n", "not in the config"),
        ("[headers.pwm1]\nmin_duty = 20\nspeed = 1\n", "not allowed"),
        ("mode = [\n", "not valid TOML"),
        ("expires_at = 1000.0\n[headers.pwm1]\nmin_duty = 20\n", "expires_at"),
        ('expires_at = 2000.0\nmode = "active"\n', "time-bounded"),
        ("expires_at = 1979-05-27T07:32:00\n", "time zone"),
        ("", "empty"),
    ],
)
def test_install_refuses_a_bad_candidate_and_leaves_the_live_file(
    host, capsys, monkeypatch, candidate, reason
):
    before = host.live.read_bytes()
    stamp = host.live.stat().st_mtime_ns
    # A real clock: expires_at 1000.0 is long past, 2000.0 is only used in the invalid cases.
    code, out = run_install(host, candidate, capsys, monkeypatch)
    assert code == 1
    assert reason in out.err
    assert host.live.read_bytes() == before
    assert host.live.stat().st_mtime_ns == stamp
    assert leftovers(host) == []


def test_install_refuses_a_file_the_service_would_ignore(host, capsys, monkeypatch):
    # The service ignores a file that is not root-owned. check-config used to exit 0 here.
    monkeypatch.setattr(
        config_module, "_stat_file",
        lambda p: SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o644),
    )
    before = host.live.read_bytes()
    code, out = run_install(host, "[headers.pwm1]\nmin_duty = 20\n", capsys, monkeypatch)
    assert code == 1
    assert "would ignore" in out.err and "not owned by root" in out.err
    assert host.live.read_bytes() == before
    assert leftovers(host) == []


def test_install_refuses_when_it_cannot_become_root(host, capsys, monkeypatch):
    def not_root(path):
        raise InstallError("cannot make the file root-owned (run as root): denied")

    monkeypatch.setattr(install_module, "chown_root", not_root)
    before = host.live.read_bytes()
    code, out = run_install(host, "[headers.pwm1]\nmin_duty = 20\n", capsys, monkeypatch)
    assert code == 1 and "root-owned" in out.err
    assert host.live.read_bytes() == before
    assert leftovers(host) == []


def test_install_refuses_an_oversized_candidate(host, capsys, monkeypatch):
    before = host.live.read_bytes()
    code, out = run_install(host, "#" * (install_module.MAX_CANDIDATE_BYTES + 1), capsys, monkeypatch)
    assert code == 1 and "larger than" in out.err
    assert host.live.read_bytes() == before


def test_install_refuses_when_the_main_config_is_invalid(host, capsys, monkeypatch):
    put(host.config, "mode = 3\n")
    before = host.live.read_bytes()
    code, out = run_install(host, LIVE, capsys, monkeypatch)
    assert code == 1 and "main config is invalid" in out.err
    assert host.live.read_bytes() == before


def test_install_signals_the_running_service(host, capsys, monkeypatch):
    lock = host.dir / "thermalctl.lock"
    put(lock, "service 4242\n")
    sent = []
    monkeypatch.setattr("thermalctl.cli.signal_service", sent.append)
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    stdin = io.TextIOWrapper(io.BytesIO(LIVE.encode()), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", stdin)
    code = main([
        "install-override", "--config", str(host.config), "--overrides", str(host.live),
        "--lock-file", str(lock),
    ])
    assert code == 0
    assert sent == [4242]
    assert "signalled" in capsys.readouterr().out


def test_install_does_not_signal_when_the_service_is_not_running(host, capsys, monkeypatch):
    sent = []
    monkeypatch.setattr("thermalctl.cli.signal_service", sent.append)
    code, out = run_install(host, LIVE, capsys, monkeypatch)
    assert code == 0 and sent == []
    assert "not running" in out.out


def test_lock_records_the_holder_pid(tmp_path):
    from thermalctl.lock import OwnerLock, holder_pid

    path = tmp_path / "x.lock"
    lock = OwnerLock(path)
    lock.acquire(role="service")
    try:
        if install_module.os.name == "posix":
            assert holder_pid(path) == install_module.os.getpid()
    finally:
        lock.release()
    assert holder_pid(tmp_path / "missing.lock") is None


def test_only_the_service_is_ever_signalled_not_a_mapping_holder(tmp_path):
    from thermalctl.lock import OwnerLock, holder_pid, is_held

    path = tmp_path / "x.lock"
    # A stale pid left by an earlier service run.
    put(path, "service 4242\n")
    mapper = OwnerLock(path)
    mapper.acquire()  # the role map-headers uses
    try:
        # SIGHUP would end a mapping run without restoring the fans, so no pid is offered.
        assert holder_pid(path) is None
    finally:
        mapper.release()


def test_install_does_not_signal_a_mapping_holder(host, capsys, monkeypatch):
    lock = host.dir / "thermalctl.lock"
    put(lock, "service 4242\n")  # stale pid from an earlier service run
    sent = []
    monkeypatch.setattr("thermalctl.cli.signal_service", sent.append)
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    from thermalctl.lock import OwnerLock

    mapper = OwnerLock(lock)
    mapper.acquire()
    try:
        code, out = run_install(host, LIVE, capsys, monkeypatch, "--lock-file", str(lock))
    finally:
        mapper.release()
    assert code == 0 and sent == []


def test_probing_a_free_lock_leaves_the_file_alone(tmp_path):
    from thermalctl.lock import is_held

    path = tmp_path / "x.lock"
    put(path, "service 4242\n")
    assert is_held(path) is False
    assert path.read_text(encoding="ascii") == "service 4242\n"


def test_install_refuses_a_mode_change_the_running_service_would_refuse(
    host, capsys, monkeypatch
):
    status = host.dir / "status.json"
    put(status, json.dumps({"mode": "dry_run"}))
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    before = host.live.read_bytes()
    code, out = run_install(
        host, 'mode = "active"\n[headers.pwm1]\nmin_duty = 22\n', capsys, monkeypatch,
        "--status-path", str(status),
    )
    assert code == 1 and "needs a restart" in out.err
    assert host.live.read_bytes() == before and leftovers(host) == []
    # The same mode as the running service is accepted.
    put(status, json.dumps({"mode": "active"}))
    code, out = run_install(
        host, 'mode = "active"\n[headers.pwm1]\nmin_duty = 22\n', capsys, monkeypatch,
        "--status-path", str(status),
    )
    assert code == 0, out.err


def test_install_refuses_a_mode_change_when_the_running_mode_is_unknown(
    host, capsys, monkeypatch
):
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    code, out = run_install(
        host, 'mode = "active"\n', capsys, monkeypatch,
        "--status-path", str(host.dir / "missing.json"),
    )
    assert code == 1 and "unknown" in out.err


def test_install_refuses_when_the_running_mode_came_from_an_earlier_override(
    host, capsys, monkeypatch
):
    # The base file says dry_run, the service runs active because an override said so. A
    # floor-only candidate would merge to dry_run, which the reload refuses.
    put(host.config, MAIN.replace('mode = "active"', 'mode = "dry_run"'))
    status = host.dir / "status.json"
    put(status, json.dumps({"mode": "active", "overrides_mode": "active"}))
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    before = host.live.read_bytes()
    code, out = run_install(
        host, "[headers.pwm1]\nmin_duty = 50\n", capsys, monkeypatch,
        "--status-path", str(status),
    )
    assert code == 1 and "needs a restart" in out.err
    assert host.live.read_bytes() == before and leftovers(host) == []


def test_install_refuses_while_the_main_config_holds_a_pending_restart(
    host, capsys, monkeypatch
):
    status = host.dir / "status.json"
    put(status, json.dumps({
        "mode": "active", "overrides_mode": None,
        "config_error": "restart required: header pwm1 path /a->/b",
    }))
    monkeypatch.setattr("thermalctl.cli.is_held", lambda path: True)
    before = host.live.read_bytes()
    code, out = run_install(
        host, "[headers.pwm1]\nmin_duty = 50\n", capsys, monkeypatch,
        "--status-path", str(status),
    )
    assert code == 1 and "restart" in out.err
    assert host.live.read_bytes() == before and leftovers(host) == []


@pytest.mark.parametrize("value", ["1e12", "1e300", "253402300800", "9999-12-31T23:59:59-05:00"])
def test_install_refuses_an_expiry_that_cannot_be_shown(host, capsys, monkeypatch, value):
    before = host.live.read_bytes()
    code, out = run_install(
        host, f"expires_at = {value}\n[headers.pwm1]\nmin_duty = 30\n", capsys, monkeypatch
    )
    assert code == 1 and "expires_at" in out.err
    assert host.live.read_bytes() == before and leftovers(host) == []


def test_status_and_check_config_survive_a_huge_expiry(tmp_path, capsys):
    document = {
        "timestamp": time.time(), "mode": "active", "config_valid": True,
        "override_active": True, "override_expires_at": 1e300, "zones": {}, "headers": {},
    }
    path = tmp_path / "status.json"
    put(path, json.dumps(document))
    assert main(["status", "--status-path", str(path)]) == 0
    assert "override active" in capsys.readouterr().out


def test_expiry_does_not_apply_an_unknown_base_that_needs_a_restart(tmp_path, caplog):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    rig.ctl._base_config = None
    put(rig.main, MAIN.replace("/fake/pwm1", "/fake/pwm9"))
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        doc = rig.cycle(advance=20.0)
    # The changed path is never applied by the expiry; the failure is loud instead.
    assert doc["headers"]["pwm1"]["min_duty"] == 20
    assert any("needs a restart" in r.getMessage() for r in caplog.records)


# -- check-config exit code -----------------------------------------------------------


def test_check_config_exits_non_zero_for_an_expired_override(host, capsys):
    put(host.live, "expires_at = 1000.0\n[headers.pwm1]\nmin_duty = 20\n")
    assert main(["check-config", str(host.config), "--overrides", str(host.live)]) == 1
    out = capsys.readouterr()
    assert "overrides: expired" in out.out and "expires_at has passed" in out.err


def test_check_config_shows_the_expiry(host, capsys):
    future = int(time.time()) + 3600
    put(host.live, f"expires_at = {future}\n[headers.pwm1]\nmin_duty = 20\n")
    assert main(["check-config", str(host.config), "--overrides", str(host.live)]) == 0
    assert "override expires at" in capsys.readouterr().out


# -- expiry ---------------------------------------------------------------------------


def test_expires_at_accepts_a_toml_datetime_with_offset(trusted, tmp_path):  # noqa: F811
    put(tmp_path / "c.toml", MAIN)
    put(tmp_path / "o.toml", "expires_at = 2030-01-01T00:00:00Z\n[headers.pwm1]\nmin_duty = 20\n")
    _, report = apply_overrides(load_config(tmp_path / "c.toml"), tmp_path / "o.toml", now=1.0)
    assert report.applied and report.expires_at == 1893456000.0


@pytest.mark.parametrize("value", ["true", '"soon"', "nan", "-5", "0"])
def test_expires_at_rejects_bad_values(trusted, tmp_path, value):  # noqa: F811
    put(tmp_path / "c.toml", MAIN)
    put(tmp_path / "o.toml", f"expires_at = {value}\n")
    with pytest.raises(ConfigError):
        apply_overrides(load_config(tmp_path / "c.toml"), tmp_path / "o.toml", now=1.0)


def test_override_reverts_at_expiry_with_an_audit_line(tmp_path, caplog):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    # The rig clock starts at 1000, so the override is active for the first cycles.
    doc = rig.cycle()
    assert doc["override_active"] is True and doc["override_expires_at"] == 1010.0
    assert doc["headers"]["pwm1"]["min_duty"] == 20
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        doc = rig.cycle(advance=5.0)
        assert doc["override_active"] is True
        assert not [r for r in caplog.records if "override ended" in r.message]
        # No file change and no signal: only the clock moved past the expiry.
        doc = rig.cycle(advance=5.0)
    assert doc["override_active"] is False and doc["override_expires_at"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    assert doc["overrides_error"] is None and doc["config_valid"] is True
    lines = [r.getMessage() for r in caplog.records]
    assert any("override ended" in line and "expires_at=1010" in line for line in lines)
    assert any("header pwm1 min_duty=20.0->40.0" in line for line in lines)
    # A later cycle does not repeat the audit line.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        rig.cycle()
    assert not [r for r in caplog.records if "override ended" in r.message]


def test_override_without_expiry_stays_active(tmp_path):
    rig = Rig(tmp_path, "[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    doc = rig.cycle(advance=100000.0)
    assert doc["override_active"] is True and doc["override_expires_at"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 20


def test_expiry_reverts_even_when_the_overrides_file_can_no_longer_be_merged(
    tmp_path, monkeypatch, caplog
):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()

    def broken(config, path, now=None):
        raise ConfigError("overrides unreadable")

    monkeypatch.setattr("thermalctl.controller.apply_overrides", broken)
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        doc = rig.cycle(advance=20.0)
    # The floor must not stay lowered past its end: the base config applies.
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    assert doc["override_active"] is False
    assert any("override ended" in r.getMessage() for r in caplog.records)


def test_expiry_reverts_when_the_main_config_needs_a_restart(tmp_path, caplog):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    # An edit the running backend cannot follow: both the normal and the forced reload
    # are refused for it, yet the lowered floor must still end at expires_at.
    put(rig.main, MAIN.replace("/fake/pwm1", "/fake/pwm9"))
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        doc = rig.cycle(advance=20.0)
    assert doc["override_active"] is False and doc["override_expires_at"] is None
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    assert doc["config_error"] and "restart required" in doc["config_error"]
    assert any("override ended" in r.getMessage() for r in caplog.records)
    # Later cycles keep the base floor and do not repeat the audit line.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        doc = rig.cycle()
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    assert not [r for r in caplog.records if "override ended" in r.getMessage()]


def test_expired_override_at_start_uses_the_base_config(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="thermalctl.audit"):
        rig = Rig(tmp_path, "expires_at = 500\n[headers.pwm1]\nmin_duty = 20\n")
    doc = rig.cycle()
    assert doc["override_active"] is False
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    assert any("expired" in r.getMessage() for r in caplog.records)


def test_installing_a_newer_override_replaces_the_expiring_one(tmp_path):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    rig.write_overrides("expires_at = 5000\n[headers.pwm1]\nmin_duty = 30\n")
    doc = rig.cycle(advance=20.0)
    assert doc["override_active"] is True and doc["override_expires_at"] == 5000.0
    assert doc["headers"]["pwm1"]["min_duty"] == 30


def test_status_command_shows_the_active_override(tmp_path, capsys):
    document = {
        "timestamp": time.time(), "mode": "active", "config_valid": True,
        "override_active": True, "override_expires_at": 1893456000.0,
        "zones": {}, "headers": {},
    }
    path = tmp_path / "status.json"
    put(path, json.dumps(document))
    assert main(["status", "--status-path", str(path)]) == 0
    assert "override active, expires 2030-01-01T00:00:00Z" in capsys.readouterr().out


def test_status_file_reports_the_override(tmp_path):
    rig = Rig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 22.5\n")
    doc = rig.cycle()
    assert doc["headers"]["pwm1"]["min_duty"] == 22.5
    assert doc["override_active"] is True


# -- packaged sudoers example and unit file --------------------------------------------


def sudoers_rules():
    text = (ROOT / "packaging" / "sudoers.d" / "hostwatch-control").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_sudoers_example_grants_only_install_override():
    rules = sudoers_rules()
    assert len(rules) == 1
    rule = rules[0]
    assert re.fullmatch(
        r"hostwatch-control ALL=\(root\) NOPASSWD: "
        r"/opt/thermalctl/venv/bin/thermalctl install-override",
        rule,
    )
    # No wildcard, no list of commands and no generic file tool.
    assert "*" not in rule and "," not in rule and "ALL" not in rule.split(":", 1)[1]
    for tool in ("tee", "mv", "cp", "rm", "sh", "bash", "vi", "sed", "cat", "env", "python"):
        assert not re.search(rf"(^|[/\s]){tool}(\s|$)", rule.split(":", 1)[1].replace(
            "/opt/thermalctl/venv/bin/thermalctl", "")), tool


def test_sudoers_example_documents_the_root_owned_path_requirement():
    text = (ROOT / "packaging" / "sudoers.d" / "hostwatch-control").read_text(encoding="utf-8")
    assert "root-owned" in text and "not writable" in text


def unit_settings():
    text = (ROOT / "packaging" / "thermalctl.service").read_text(encoding="utf-8")
    return {
        key: value
        for key, _, value in (
            ln.partition("=") for ln in text.splitlines() if "=" in ln and not ln.startswith("#")
        )
    }


def test_unit_has_hardening_that_leaves_sysfs_writable():
    unit = unit_settings()
    assert unit["NoNewPrivileges"] == "yes"
    assert unit["ProtectSystem"] == "full"
    assert unit["RestrictAddressFamilies"] == "AF_UNIX"
    # These can make /sys read-only or hide it, which would stop fan duty writes.
    for forbidden in ("ProtectKernelTunables", "PrivateDevices", "ReadOnlyPaths", "InaccessiblePaths"):
        assert forbidden not in unit
    assert unit["ProtectSystem"] != "strict"
    # The notify socket and the status and state files must still work.
    assert unit["NotifyAccess"] == "main" and unit["RuntimeDirectory"] == "thermalctl"
    assert unit["User"] == "root"


def test_unit_hardening_is_listed_as_unverified():
    text = (ROOT / "UNVERIFIED.md").read_text(encoding="utf-8")
    assert "NoNewPrivileges=yes" in text and "install-override" in text


class SplitClockRig:
    """A controller whose wall clock and monotonic clock are moved independently."""

    def __init__(self, tmp_path, overrides_text):
        self.mono = 5000.0
        self.wall = 1000.0
        self.main = tmp_path / "config.toml"
        self.over = tmp_path / "overrides.toml"
        self.status = tmp_path / "status.json"
        put(self.main, MAIN)
        put(self.over, overrides_text)
        config, report = config_module.load_effective(self.main, self.over, now=self.wall)
        self.backend = FakeBackend()
        self.backend.rpms = {"pwm1": 900.0}
        self.ctl = Controller(
            config, self.backend, status_path=self.status, clock=lambda: self.mono,
            wall_clock=lambda: self.wall, hold_s=5.0, ema_alpha=1.0, ramp_down_per_s=2.0,
            overrides_path=self.over, config_path=self.main, overrides_report=report,
        )

    def cycle(self, advance=1.0, wall_step=0.0):
        self.mono += advance
        self.wall += advance + wall_step
        self.backend.inputs = {"temp": Reading(30.0, self.mono)}
        self.ctl.cycle()
        return json.loads(self.status.read_text(encoding="utf-8"))


def test_backward_wall_step_does_not_extend_a_lowered_override(tmp_path):
    rig = SplitClockRig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 20
    # The wall clock jumps back 30 s, inside the drift tolerance. By wall time the override
    # now has about 40 s left, but only 10 s were granted from install.
    doc = rig.cycle(wall_step=-30.0)
    assert 1010.0 - rig.wall > 30.0
    assert doc["override_active"] is True
    ended = None
    for _ in range(15):
        doc = rig.cycle()
        if not doc["override_active"]:
            ended = rig.mono
            break
    # Monotonic time since install passed the 10 s the file allowed, so it ended on time.
    assert ended is not None and ended - 5000.0 <= 12.0
    assert doc["headers"]["pwm1"]["min_duty"] == 40
    # A reload under the stepped-back wall clock must not bring the override back.
    rig.ctl.request_reload()
    doc = rig.cycle()
    assert doc["override_active"] is False and doc["headers"]["pwm1"]["min_duty"] == 40
    put(rig.over, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 22\n")
    rig.ctl.request_reload()
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 40


def test_wall_clock_far_from_monotonic_time_reverts_to_the_base_config(tmp_path):
    rig = SplitClockRig(tmp_path, "expires_at = 100000\n[headers.pwm1]\nmin_duty = 20\n")
    assert rig.cycle()["headers"]["pwm1"]["min_duty"] == 20
    doc = rig.cycle(wall_step=-500.0)
    assert doc["override_active"] is False
    assert doc["headers"]["pwm1"]["min_duty"] == 40


def test_forward_wall_step_still_ends_an_override_early(tmp_path):
    rig = SplitClockRig(tmp_path, "expires_at = 1010\n[headers.pwm1]\nmin_duty = 20\n")
    rig.cycle()
    doc = rig.cycle(wall_step=100.0)
    assert doc["override_active"] is False and doc["headers"]["pwm1"]["min_duty"] == 40
