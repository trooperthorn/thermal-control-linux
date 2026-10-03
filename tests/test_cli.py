import json
import re
import socket
import time
from pathlib import Path

import pytest

from thermalctl import __version__, cli
from thermalctl.__main__ import main
from thermalctl.load import LoadBackend
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


def test_load_backend_missing_proc_stat_gives_no_value(tmp_path):
    backend = LoadBackend(_NoInputs(), str(tmp_path / "nope"))
    assert backend.read_inputs()["cpu_load_percent"].value is None


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
