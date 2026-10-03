"""Command line: run, status, check-config, restore and map-headers.

Every command returns a process exit code instead of raising, so the systemd unit and
the tests see the same behaviour. `run` is dry run unless the config says active.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .backends.sysfs import (
    BackendError,
    StateFileError,
    SysfsBackend,
    install_signal_handlers,
    restore_from_state_file,
)
from .config import Config, ConfigError, load_config
from .controller import DEFAULT_STATUS_PATH, Controller
from .hwmon import DEFAULT_HWMON_ROOT, HwmonError, resolve_config
from .load import SUPPORTED_LOAD_INPUTS, LoadBackend
from .lock import LockHeld, OwnerLock, default_lock_path, is_held
from .mapping import plan_lines, run_mapping
from .notify import Notifier

DEFAULT_STATE_FILE = "/run/thermalctl/state.json"
DEFAULT_INTERVAL_S = 2.0
DEFAULT_MAX_AGE_S = 30.0
MAPPING_SETTLE_S = 8.0

log = logging.getLogger("thermalctl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="thermalctl", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the control loop (dry run unless the config is active)")
    run.add_argument("--config", required=True, metavar="PATH")
    run.add_argument("--status-path", default=DEFAULT_STATUS_PATH)
    run.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    run.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds per cycle")
    run.add_argument("--lock-file", default=None, help="ownership lock (default: beside the state file)")
    run.add_argument("--hwmon-root", default=DEFAULT_HWMON_ROOT, help=argparse.SUPPRESS)

    status = sub.add_parser("status", help="print the status file the service writes")
    status.add_argument("--status-path", default=DEFAULT_STATUS_PATH)
    status.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S)
    status.add_argument("--json", action="store_true", help="print the raw document")

    check = sub.add_parser("check-config", help="validate a config file and change nothing")
    check.add_argument("path", metavar="PATH")

    restore = sub.add_parser("restore", help="restore the persisted original fan modes")
    restore.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    restore.add_argument("--lock-file", default=None, help="ownership lock (default: beside the state file)")
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even if the lock is held; for ExecStopPost, which runs after the service exits",
    )

    mapping = sub.add_parser("map-headers", help="guided test of which fan each header drives")
    mapping.add_argument("--config", required=True, metavar="PATH")
    mapping.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    mapping.add_argument("--lock-file", default=None, help="ownership lock (default: beside the state file)")
    mapping.add_argument("--hwmon-root", default=DEFAULT_HWMON_ROOT, help=argparse.SUPPRESS)
    mapping.add_argument(
        "--apply",
        action="store_true",
        help="really lower each header (needs a terminal); without it only the plan is printed",
    )
    return parser


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(f"thermalctl: {text}", file=sys.stderr)


def validate_for_service(config: Config) -> list[str]:
    """Problems that load_config does not catch but that would starve a zone of input."""
    problems = []
    for zone in config.zones:
        if zone.load_input is not None and zone.load_input not in SUPPORTED_LOAD_INPUTS:
            problems.append(
                f"zone {zone.id}: load_input {zone.load_input!r} is not one of "
                + ", ".join(SUPPORTED_LOAD_INPUTS)
            )
    return problems


def build_backend(config: Config, state_file: str):
    temperature_inputs = {z.temperature_input: z.temperature_input for z in config.zones}
    sysfs = SysfsBackend(
        {h.id: h.path for h in config.headers},
        temperature_inputs,
        [h.id for h in config.headers if h.mapped],
        state_file,
        active=config.mode == "active",
    )
    return sysfs


def run_service(
    config_path: str,
    status_path: str,
    state_file: str,
    interval_s: float,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    notifier: Notifier | None = None,
    proc_stat: str = "/proc/stat",
    lock_file: str | None = None,
    hwmon_root: str = DEFAULT_HWMON_ROOT,
) -> int:
    notifier = Notifier() if notifier is None else notifier
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _err(f"invalid config, fans stay under firmware control: {exc}")
        return 1
    problems = validate_for_service(config)
    if problems:
        for problem in problems:
            _err(problem)
        return 1
    # Resolve chip names before anything else, so a missing chip leaves every fan alone.
    try:
        config = resolve_config(config, hwmon_root)
    except HwmonError as exc:
        _err(f"cannot find fan hardware, fans stay under firmware control: {exc}")
        return 1
    lock = OwnerLock(lock_file or default_lock_path(state_file))
    try:
        lock.acquire(create_dir=True)
    except (LockHeld, OSError) as exc:
        _err(f"cannot take the ownership lock: {exc}")
        return 1
    try:
        return _serve(
            config, config_path, status_path, state_file, interval_s,
            should_stop, sleep, notifier, proc_stat, hwmon_root,
        )
    finally:
        lock.release()


def _serve(
    config: Config,
    config_path: str,
    status_path: str,
    state_file: str,
    interval_s: float,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None],
    notifier: Notifier,
    proc_stat: str,
    hwmon_root: str,
) -> int:
    if config.mode != "active":
        log.info("mode is dry_run: computing and logging only, no hardware writes")
    sysfs = build_backend(config, state_file)
    install_signal_handlers()

    def tick(seconds: float) -> None:
        notifier.watchdog()
        sleep(seconds)

    try:
        with sysfs:
            backend = LoadBackend(sysfs, proc_stat)
            controller = Controller(
                config, backend, status_path=status_path,
                config_transform=lambda c: resolve_config(c, hwmon_root),
            )
            notifier.ready()
            try:
                controller.run(interval_s, should_stop, sleep=tick)
            finally:
                notifier.stopping()
    except BackendError as exc:
        _err(f"cannot start: {exc}")
        return 1
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        text = Path(args.status_path).read_text(encoding="utf-8")
        doc = json.loads(text)
        stamp = float(doc["timestamp"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _err(f"cannot read status file {args.status_path}: {exc}")
        return 1
    age = time.time() - stamp
    if args.json:
        _out(text.rstrip("\n"))
    else:
        _out(f"mode {doc.get('mode')}, config valid {doc.get('config_valid')}, age {age:.0f} s")
        # The inputs each zone is reacting to, so the duties below can be judged against them.
        # A missing value prints as "unavailable", never as zero.
        for zid, zone in sorted(doc.get("zones", {}).items()):
            temp, load = zone.get("temperature"), zone.get("load")
            temp_text = "unavailable" if temp is None else f"{temp:.1f} C"
            load_text = "not used" if "load" not in zone or (load is None and zone.get("load_curve") is None) \
                else ("unavailable" if load is None else f"{load:.0f} %")
            _out(f"  zone {zid}: temperature {temp_text}, load {load_text}")
        for hid, header in sorted(doc.get("headers", {}).items()):
            reasons = ",".join(header.get("reasons", [])) or "none"
            _out(
                f"  {hid}: {header.get('state')} duty {header.get('duty')} "
                f"rpm {header.get('rpm')} reasons {reasons}"
            )
    if age > args.max_age:
        _err(f"status is stale: {age:.0f} s old, limit {args.max_age:g} s")
        return 1
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.path)
    except ConfigError as exc:
        _err(f"invalid: {exc}")
        return 1
    problems = validate_for_service(config)
    if problems:
        for problem in problems:
            _err(problem)
        return 1
    mapped = [h.id for h in config.headers if h.mapped]
    _out(f"ok: mode {config.mode}, {len(config.zones)} zones, {len(config.headers)} headers")
    _out(f"mapped headers: {', '.join(mapped) if mapped else 'none'}")
    if config.mode == "active" and not mapped:
        _out("note: active mode controls nothing until a header is mapped")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    lock_path = args.lock_file or default_lock_path(args.state_file)
    if not args.force and is_held(lock_path):
        _err(
            f"the service holds {lock_path}; stop it first, or use --force "
            "(only for ExecStopPost, after the service has exited)"
        )
        return 1
    try:
        fallbacks = restore_from_state_file(args.state_file)
    except StateFileError as exc:
        _err(f"cannot restore, fans may still be in manual mode: {exc}")
        return 1
    if fallbacks:
        _err("restore failed, wrote full speed instead for: " + ", ".join(sorted(fallbacks)))
        return 1
    _out("restore complete or nothing to restore")
    return 0


def cmd_map_headers(
    args: argparse.Namespace,
    *,
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
    ask: Callable[[str], str] = input,
    settle: Callable[[], None] | None = None,
) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _err(f"invalid config: {exc}")
        return 1
    try:
        config = resolve_config(config, args.hwmon_root)
    except HwmonError as exc:
        _err(f"cannot find fan hardware: {exc}")
        return 1
    for line in plan_lines(config):
        _out(line)
    if not args.apply:
        _out("Dry run: nothing was written. Add --apply on a terminal to run the test.")
        return 0
    if not is_tty():
        _err("refusing to lower fans without a terminal; run this from an interactive shell")
        return 1
    if settle is None:
        def settle() -> None:
            time.sleep(MAPPING_SETTLE_S)
    lock = OwnerLock(args.lock_file or default_lock_path(args.state_file))
    try:
        lock.acquire()
    except LockHeld:
        _err(f"the service holds {lock.path}; stop it before running the mapping test")
        return 1
    except OSError as exc:
        _err(f"cannot take the ownership lock: {exc}")
        return 1
    install_signal_handlers()
    try:
        run_mapping(config, args.state_file, ask=ask, say=_out, settle=settle)
    except (BackendError, StateFileError) as exc:
        _err(f"mapping stopped: {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        _err("mapping interrupted, original modes restored")
        return 1
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        lock.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        _out(__version__)
        return 0
    if args.command is None:
        build_parser().print_usage(sys.stderr)
        return 2
    if args.command == "run":
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
        if args.interval <= 0:
            _err("--interval must be positive")
            return 2
        return run_service(
            args.config, args.status_path, args.state_file, args.interval,
            lock_file=args.lock_file, hwmon_root=args.hwmon_root,
        )
    if args.command == "status":
        return cmd_status(args)
    if args.command == "check-config":
        return cmd_check_config(args)
    if args.command == "restore":
        return cmd_restore(args)
    return cmd_map_headers(args)
