import json
import re
import socket
import time
from pathlib import Path

import pytest

from thermalctl import __version__, cli
from thermalctl.__main__ import main
from thermalctl.controller import Controller
from thermalctl.load import LoadBackend
from thermalctl.lock import OwnerLock, is_held
from thermalctl.notify import Notifier

ROOT = Path(__file__).resolve().parent.parent


def put(path, text):
    Path(path).write_text(text, encoding="ascii", newline="\n")


def subcommands():
    return cli.build_parser()._subparsers._group_actions[0].choices


def make_tree(tmp_path, enables=(5, 2)):
    d = tmp_path / "hw"
    d.mkdir()
    for n, enable in enumerate(enables, start=1):
        put(d / f"pwm{n}", "128\n")
        put(d / f"pwm{n}_enable", f"{enable}\n")
        put(d / f"fan{n}_input", "900\n")
    put(d / "temp1_input", "45500\n")
    return d


def write_config(tmp_path, tree, mode="dry_run", mapped="true", load=True):
    load_lines = (
        'load_input = "cpu_load_percent"\nload_curve = [[0, 0], [90, 100]]\n' if load else ""
    )
    text = f"""mode = "{mode}"
[[zones]]
id = "cpu"
temperature_input = "{(tree / 'temp1_input').as_posix()}"
temperature_curve = [[40, 20], [80, 100]]
{load_lines}hard_max_temp_c = 90
stale_after_s = 10
"""
    for n in (1, 2):
        text += f"""
[[headers]]
id = "pwm{n}"
path = "{(tree / f'pwm{n}').as_posix()}"
mapped = {mapped}
min_duty = 20
min_rpm = 300
stall_window_s = 15
zones = ["cpu"]
"""
    path = tmp_path / "config.toml"
    put(path, text)
    return path


def write_proc_stat(tmp_path, busy, idle):
    path = tmp_path / "stat"
    put(path, f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n")
    return path


# -- argument handling ----------------------------------------------------------------


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_no_command_is_usage_error(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [["run"], ["check-config"], ["map-headers"], ["bogus"], ["run", "--config"]],
)
def test_bad_arguments_exit_two(argv, capsys):
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2


def test_run_rejects_non_positive_interval(tmp_path):
    assert main(["run", "--config", str(tmp_path / "c.toml"), "--interval", "0"]) == 2


def test_run_parses_options(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "run_service", fake)
    argv = ["run", "--config", "c.toml", "--status-path", "s", "--state-file", "t",
            "--interval", "5"]
    assert main(argv) == 0
    assert seen["args"] == ("c.toml", "s", "t", 5.0)


def test_check_config_ok_and_invalid(tmp_path, capsys):
    tree = make_tree(tmp_path)
    good = write_config(tmp_path, tree)
    assert main(["check-config", str(good)]) == 0
    assert "ok: mode dry_run" in capsys.readouterr().out
    bad = tmp_path / "bad.toml"
    put(bad, 'mode = "turbo"\n')
    assert main(["check-config", str(bad)]) == 1
    assert main(["check-config", str(tmp_path / "missing.toml")]) == 1


def test_check_config_rejects_unknown_load_input(tmp_path, capsys):
    tree = make_tree(tmp_path)
    path = write_config(tmp_path, tree)
    put(path, path.read_text().replace("cpu_load_percent", "gpu_load"))
    assert main(["check-config", str(path)]) == 1
    assert "gpu_load" in capsys.readouterr().err


def test_status_reads_file_and_flags_stale(tmp_path, capsys):
    status = tmp_path / "status.json"
    doc = {"timestamp": time.time(), "mode": "dry_run", "config_valid": True,
           "headers": {"pwm1": {"state": "dry_run", "duty": 30, "rpm": 900, "reasons": []}}}
    put(status, json.dumps(doc))
    assert main(["status", "--status-path", str(status)]) == 0
    assert "pwm1: dry_run" in capsys.readouterr().out
    assert main(["status", "--status-path", str(status), "--json"]) == 0
    doc["timestamp"] = time.time() - 1000
    put(status, json.dumps(doc))
    assert main(["status", "--status-path", str(status)]) == 1
    assert main(["status", "--status-path", str(tmp_path / "none.json")]) == 1


# -- run ----------------------------------------------------------------------------


def run_cycles(tmp_path, config, cycles, notifier=None):
    status = tmp_path / "status.json"
    state = tmp_path / "state.json"
    proc = write_proc_stat(tmp_path, 100, 900)
    count = {"n": 0}

    def should_stop():
        count["n"] += 1
        return count["n"] > cycles

    code = cli.run_service(
        str(config), str(status), str(state), 1.0,
        should_stop=should_stop, sleep=lambda s: None, notifier=notifier,
        proc_stat=str(proc),
    )
    return code, status, state


def test_dry_run_never_writes_hardware(tmp_path):
    tree = make_tree(tmp_path)
    code, status, state = run_cycles(tmp_path, write_config(tmp_path, tree), 2)
    assert code == 0
    assert (tree / "pwm1").read_text() == "128\n"
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()
    assert json.loads(status.read_text())["mode"] == "dry_run"


def test_active_run_restores_originals_on_exit(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree, mode="active")
    code, status, state = run_cycles(tmp_path, config, 2)
    assert code == 0
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert (tree / "pwm2_enable").read_text() == "2\n"
    assert not state.exists()
    assert (tree / "pwm1").read_text() != "128\n"  # a duty was written while active


def test_run_with_invalid_config_touches_nothing(tmp_path):
    tree = make_tree(tmp_path)
    bad = tmp_path / "bad.toml"
    put(bad, "mode = 3\n")
    code, status, state = run_cycles(tmp_path, bad, 1)
    assert code == 1
    assert (tree / "pwm1_enable").read_text() == "5\n"


def test_run_sends_notifications(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    sent = []

    class Spy(Notifier):
        def send(self, message):
            sent.append(message)
            return True

    run_cycles(tmp_path, config, 3, notifier=Spy({}))
    assert sent[0] == "READY=1"
    assert sent.count("WATCHDOG=1") == 3
    assert sent[-1] == "STOPPING=1"


# -- restore -------------------------------------------------------------------------


def test_restore_uses_persisted_originals(tmp_path):
    tree = make_tree(tmp_path)
    state = tmp_path / "state.json"
    put(state, json.dumps({
        "headers": {"pwm1": str(tree / "pwm1"), "pwm2": str(tree / "pwm2")},
        "originals": {"pwm1": 5, "pwm2": 2},
    }))
    put(tree / "pwm1_enable", "1\n")
    put(tree / "pwm2_enable", "1\n")
    assert main(["restore", "--state-file", str(state)]) == 0
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert (tree / "pwm2_enable").read_text() == "2\n"
    assert not state.exists()
    # A second call has nothing to restore and still succeeds.
    assert main(["restore", "--state-file", str(state)]) == 0


def test_restore_fails_loudly_on_bad_state_file(tmp_path, capsys):
    state = tmp_path / "state.json"
    put(state, "not json")
    assert main(["restore", "--state-file", str(state)]) == 1
    assert "manual mode" in capsys.readouterr().err


def test_restore_after_a_killed_service(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree, mode="active")
    state = tmp_path / "state.json"
    sysfs = cli.build_backend(cli.load_config(config), str(state))
    sysfs.start()  # the service took control and was then killed before it could restore
    assert (tree / "pwm1_enable").read_text() == "1\n"
    assert main(["restore", "--state-file", str(state)]) == 0
    assert (tree / "pwm1_enable").read_text() == "5\n"


# -- map-headers ---------------------------------------------------------------------


def test_map_headers_default_prints_plan_only(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    state = tmp_path / "state.json"
    code = main(["map-headers", "--config", str(config), "--state-file", str(state)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Mapping plan" in out and "pwm1" in out and "Dry run" in out
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()


def test_map_headers_apply_refuses_without_tty(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    state = tmp_path / "state.json"
    args = cli.build_parser().parse_args(
        ["map-headers", "--config", str(config), "--state-file", str(state), "--apply"]
    )
    code = cli.cmd_map_headers(args, is_tty=lambda: False)
    assert code == 1
    assert "without a terminal" in capsys.readouterr().err
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()


def test_map_headers_apply_lowers_one_header_and_restores(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    state = tmp_path / "state.json"
    args = cli.build_parser().parse_args(
        ["map-headers", "--config", str(config), "--state-file", str(state), "--apply"]
    )
    seen = []

    def settle():
        # While pwm1 is lowered it is in manual mode and fan1 falls.
        seen.append((tree / "pwm1_enable").read_text())
        put(tree / "fan1_input", "300\n")

    answers = iter(["", "s"])
    code = cli.cmd_map_headers(args, is_tty=lambda: True, ask=lambda p: next(answers),
                               settle=settle)
    out = capsys.readouterr().out
    assert code == 0
    assert seen == ["1\n"]
    assert "fan1_input (down 600 RPM)" in out
    assert "pwm2: skipped" in out
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert (tree / "pwm2_enable").read_text() == "2\n"
    assert not state.exists()


def test_map_headers_restores_when_interrupted(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    state = tmp_path / "state.json"
    args = cli.build_parser().parse_args(
        ["map-headers", "--config", str(config), "--state-file", str(state), "--apply"]
    )

    def interrupt():
        raise KeyboardInterrupt

    code = cli.cmd_map_headers(args, is_tty=lambda: True, ask=lambda p: "", settle=interrupt)
    assert code == 1
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()


# -- load input and notifier ----------------------------------------------------------


class _NoInputs:
    def read_inputs(self):
        return {}


def test_load_backend_reports_busy_share(tmp_path):
    proc = write_proc_stat(tmp_path, 100, 900)
    backend = LoadBackend(_NoInputs(), str(proc), clock=lambda: 7.0)
    assert backend.read_inputs()["cpu_load_percent"].value is None  # no time has passed yet
    write_proc_stat(tmp_path, 150, 950)  # 50 busy jiffies of 100 since the last sample
    reading = backend.read_inputs()["cpu_load_percent"]
    assert reading.value == pytest.approx(50.0)
    assert reading.timestamp == 7.0


def test_load_backend_first_read_is_warming_up_then_uses_delta(tmp_path):
    proc = write_proc_stat(tmp_path, 100, 900)
    backend = LoadBackend(_NoInputs(), str(proc), clock=lambda: 7.0)
    write_proc_stat(tmp_path, 150, 950)  # a real delta exists, but the first cycle ignores it
    first = backend.read_inputs()["cpu_load_percent"]
    assert first.value is None and first.warming_up
    write_proc_stat(tmp_path, 200, 1000)
    second = backend.read_inputs()["cpu_load_percent"]
    assert second.value == pytest.approx(50.0) and not second.warming_up


def test_load_backend_missing_proc_stat_gives_no_value(tmp_path):
    backend = LoadBackend(_NoInputs(), str(tmp_path / "nope"))
    reading = backend.read_inputs()["cpu_load_percent"]
    assert reading.value is None and not reading.warming_up  # a fault, not warm-up


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs unix sockets")
def test_notifier_sends_datagram(tmp_path):
    path = str(tmp_path / "n.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    try:
        assert Notifier({"NOTIFY_SOCKET": path}).watchdog() is True
        assert server.recv(100) == b"WATCHDOG=1"
    finally:
        server.close()


def test_notifier_disabled_without_socket():
    notifier = Notifier({})
    assert not notifier.enabled
    assert notifier.ready() is False


# -- packaging and docs -------------------------------------------------------------


def unit_text():
    return (ROOT / "packaging" / "thermalctl.service").read_text(encoding="utf-8")


def test_unit_file_is_kill_safe():
    text = unit_text()
    assert re.search(r"^ExecStopPost=\S*thermalctl restore", text, re.M)
    assert re.search(r"^WatchdogSec=\d+", text, re.M)
    assert re.search(r"^Restart=always$", text, re.M)
    assert re.search(r"^Type=notify$", text, re.M)
    assert "RuntimeDirectory=thermalctl" in text


def test_unit_commands_are_real_subcommands():
    found = re.findall(r"^Exec\w+=\S*thermalctl (\S+)", unit_text(), re.M)
    assert {"run", "restore"} <= set(found)
    assert set(found) <= set(subcommands())


def test_unit_watchdog_outlasts_cycle_interval():
    watchdog = int(re.search(r"^WatchdogSec=(\d+)", unit_text(), re.M).group(1))
    assert watchdog > 2 * cli.DEFAULT_INTERVAL_S


def test_readme_references_only_real_commands():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    used = set(re.findall(r"thermalctl ([a-z][a-z-]*)", text))
    assert used == set(subcommands())
    options = {
        option
        for sub in subcommands().values()
        for action in sub._actions
        for option in action.option_strings
    }
    flags = set()
    for line in text.splitlines():
        if "thermalctl" in line and "systemctl" not in line:
            flags |= set(re.findall(r"(--[a-z][a-z-]*)", line))
    assert flags
    assert flags <= options, flags - options


# -- ownership lock ------------------------------------------------------------------


def killed_service_state(tmp_path):
    """A tree and state file as a killed active service leaves them (pwm1 in manual)."""
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree, mode="active")
    state = tmp_path / "state.json"
    cli.build_backend(cli.load_config(config), str(state)).start()
    assert (tree / "pwm1_enable").read_text() == "1\n"
    return tree, config, state


def test_restore_refuses_while_the_lock_is_held_and_force_works(tmp_path, capsys):
    tree, _config, state = killed_service_state(tmp_path)
    holder = OwnerLock(tmp_path / "thermalctl.lock")
    holder.acquire()
    try:
        assert main(["restore", "--state-file", str(state)]) == 1
        assert "holds" in capsys.readouterr().err
        assert (tree / "pwm1_enable").read_text() == "1\n"
        assert state.exists()
        assert main(["restore", "--state-file", str(state), "--force"]) == 0
        assert (tree / "pwm1_enable").read_text() == "5\n"
    finally:
        holder.release()


def test_restore_works_once_the_lock_is_released(tmp_path):
    tree, _config, state = killed_service_state(tmp_path)
    holder = OwnerLock(tmp_path / "thermalctl.lock")
    holder.acquire()
    holder.release()
    assert main(["restore", "--state-file", str(state)]) == 0
    assert (tree / "pwm1_enable").read_text() == "5\n"


def test_map_headers_apply_refuses_while_the_lock_is_held(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree)
    state = tmp_path / "state.json"
    args = cli.build_parser().parse_args(
        ["map-headers", "--config", str(config), "--state-file", str(state), "--apply"]
    )
    holder = OwnerLock(tmp_path / "thermalctl.lock")
    holder.acquire()
    asked = []
    try:
        code = cli.cmd_map_headers(args, is_tty=lambda: True, ask=lambda p: asked.append(p) or "")
    finally:
        holder.release()
    assert code == 1
    assert "holds" in capsys.readouterr().err
    assert asked == []
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()


def test_run_holds_the_lock_while_running_and_releases_it_after(tmp_path):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree, mode="active")
    lock_path = tmp_path / "thermalctl.lock"
    seen = []

    def stop():
        seen.append(is_held(lock_path))
        return len(seen) > 1

    status, state = tmp_path / "status.json", tmp_path / "state.json"
    proc = write_proc_stat(tmp_path, 100, 900)
    code = cli.run_service(
        str(config), str(status), str(state), 1.0, should_stop=stop,
        sleep=lambda s: None, proc_stat=str(proc),
    )
    assert code == 0
    assert seen[0] is True
    assert not is_held(lock_path)


def test_second_service_cannot_start_while_the_lock_is_held(tmp_path, capsys):
    tree = make_tree(tmp_path)
    config = write_config(tmp_path, tree, mode="active")
    holder = OwnerLock(tmp_path / "thermalctl.lock")
    holder.acquire()
    try:
        code, _status, state = run_cycles(tmp_path, config, 1)
    finally:
        holder.release()
    assert code == 1
    assert "ownership lock" in capsys.readouterr().err
    assert (tree / "pwm1_enable").read_text() == "5\n"
    assert not state.exists()


def test_unit_restore_uses_force():
    text = (ROOT / "packaging" / "thermalctl.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecStopPost=\S*thermalctl restore --force$", text, re.M)


# -- external change of pwm_enable -----------------------------------------------------


def test_external_pwm_enable_change_puts_the_header_in_failsafe(tmp_path):
    tree = make_tree(tmp_path)
    config = cli.load_config(write_config(tmp_path, tree, mode="active", load=False))
    sysfs = cli.build_backend(config, str(tmp_path / "state.json"))
    sysfs.clock = lambda: 1000.0  # one fixed time for readings and controller, so none are stale
    with sysfs:
        ctl = Controller(config, sysfs, status_path=tmp_path / "status.json", clock=sysfs.clock)
        doc = ctl.cycle()
        assert doc["headers"]["pwm1"]["state"] == "active"
        put(tree / "pwm1_enable", "2\n")  # something else took the header
        doc = ctl.cycle()
        assert doc["headers"]["pwm1"]["state"] == "failsafe"
        assert doc["headers"]["pwm1"]["reasons"] == ["external_change:pwm1"]
        assert (tree / "pwm1").read_text().strip() == "255"


def test_our_own_release_is_not_an_external_change(tmp_path):
    tree = make_tree(tmp_path)
    config = cli.load_config(write_config(tmp_path, tree, mode="active", load=False))
    sysfs = cli.build_backend(config, str(tmp_path / "state.json"))
    with sysfs:
        sysfs.release("pwm1")
        assert sysfs.owns("pwm1")
        put(tree / "pwm1_enable", "1\n")
        assert not sysfs.owns("pwm1")


# -- chip names ----------------------------------------------------------------------


def make_hwmon_root(tmp_path, layout):
    """layout maps a hwmon directory name to (chip name, number of pwm headers)."""
    root = tmp_path / "hwmon_root"
    for directory, (chip, pwms) in layout.items():
        d = root / directory
        d.mkdir(parents=True)
        put(d / "name", chip + "\n")
        put(d / "temp1_input", "45500\n")
        for n in range(1, pwms + 1):
            put(d / f"pwm{n}", "128\n")
            put(d / f"pwm{n}_enable", "5\n")
            put(d / f"fan{n}_input", "900\n")
    return root


def write_named_config(tmp_path, mode="active"):
    text = f"""mode = "{mode}"
[[zones]]
id = "cpu"
temperature_input = "coretemp:temp1_input"
temperature_curve = [[40, 20], [80, 100]]
hard_max_temp_c = 90
stale_after_s = 10

[[headers]]
id = "pwm2"
path = "nct6779:pwm2"
mapped = true
min_duty = 20
min_rpm = 300
stall_window_s = 15
zones = ["cpu"]
"""
    path = tmp_path / "named.toml"
    put(path, text)
    return path


def run_named(tmp_path, root, config, cycles=2):
    count = {"n": 0}

    def stop():
        count["n"] += 1
        return count["n"] > cycles

    return cli.run_service(
        str(config), str(tmp_path / "status.json"), str(tmp_path / "run" / "state.json"), 1.0,
        should_stop=stop, sleep=lambda s: None, hwmon_root=str(root),
        proc_stat=str(write_proc_stat(tmp_path, 100, 900)),
    )


def test_service_finds_the_chip_after_the_hwmon_index_moved(tmp_path):
    root = make_hwmon_root(
        tmp_path, {"hwmon0": ("nct6779", 5), "hwmon1": ("asus", 0), "hwmon3": ("coretemp", 0)}
    )
    assert run_named(tmp_path, root, write_named_config(tmp_path)) == 0
    assert (root / "hwmon0" / "pwm2").read_text().strip() != "128"  # driven while active
    assert (root / "hwmon0" / "pwm2_enable").read_text() == "5\n"  # restored on exit
    assert (root / "hwmon0" / "pwm1").read_text() == "128\n"  # other channels untouched


def test_missing_chip_fails_safe_and_touches_nothing(tmp_path, capsys):
    root = make_hwmon_root(tmp_path, {"hwmon0": ("asus", 3), "hwmon1": ("coretemp", 2)})
    assert run_named(tmp_path, root, write_named_config(tmp_path)) == 1
    assert "nct6779" in capsys.readouterr().err
    for directory in ("hwmon0", "hwmon1"):
        for n in (1, 2, 3):
            f = root / directory / f"pwm{n}_enable"
            if f.exists():
                assert f.read_text() == "5\n"
    assert not (tmp_path / "run").exists()
    assert not (tmp_path / "status.json").exists()


def test_map_headers_resolves_chip_names(tmp_path, capsys):
    root = make_hwmon_root(tmp_path, {"hwmon4": ("nct6779", 2), "hwmon1": ("coretemp", 0)})
    config = write_named_config(tmp_path, mode="dry_run")
    code = main(["map-headers", "--config", str(config), "--hwmon-root", str(root),
                 "--state-file", str(tmp_path / "state.json")])
    assert code == 0
    assert "hwmon4" in capsys.readouterr().out
