"""sysfs hwmon backend that hands fans back to firmware control on exit.

The backend implements the same interface as `thermalctl.backend.Backend`. It writes
only to headers that are both mapped and enabled by an active-mode config, and it
records each header's original `pwmN_enable` before the first write. The originals are
also saved to a state file, so a separate helper can restore them after the service was
killed. If restoring the original fails, the header is set to full speed instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import signal
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

from ..safety import Reading

log = logging.getLogger("thermalctl.sysfs")

MANUAL = 1
FULL_PWM = 255


class BackendError(Exception):
    """Raised when the backend cannot start safely."""


class StateFileError(Exception):
    """Raised when a state file exists but cannot be used to restore fans."""


def duty_to_pwm(duty: float) -> int:
    """Scale a duty percent to 0 to 255, clamping. A non-finite or non-number duty means full speed."""
    if not isinstance(duty, (int, float)) or not math.isfinite(duty):
        return FULL_PWM
    return max(0, min(FULL_PWM, round(duty / 100.0 * FULL_PWM)))


def _read_text(path: str) -> str:
    with open(path, encoding="ascii") as handle:
        return handle.read().strip()


def _write_int(path: str, value: int) -> None:
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(f"{value}\n")


def _enable_path(pwm_path: str) -> str:
    return pwm_path + "_enable"


def _fan_path(pwm_path: str) -> str:
    directory, name = os.path.split(pwm_path)
    return os.path.join(directory, "fan" + name[len("pwm"):] + "_input")


class SysfsBackend:
    """Reads sensors and fans and drives pwm files, restoring firmware mode on exit.

    headers maps a header id to its pwmN path. inputs maps an input name, as used in
    zone config, to a file holding a value that is divided by input_scale (millidegrees
    Celsius by default). mapped lists the header ids that passed the mapping test.
    """

    def __init__(
        self,
        headers: Mapping[str, str],
        inputs: Mapping[str, str],
        mapped: Iterable[str],
        state_file: str | Path,
        active: bool = False,
        clock: Callable[[], float] = time.time,
        input_scale: float = 1000.0,
    ) -> None:
        self.headers = dict(headers)
        self.inputs = dict(inputs)
        self.mapped = set(mapped) & set(self.headers)
        self.state_file = Path(state_file)
        self.active = active
        self.clock = clock
        self.input_scale = input_scale
        self.originals: dict[str, int] = {}
        self.restore_failures: list[str] = []
        # The pwmN_enable value this backend last set per header; anything else is foreign.
        self.expected: dict[str, int] = {}
        self.started = False

    def _controlled(self, header_id: str) -> bool:
        return self.active and header_id in self.mapped and header_id in self.originals

    # Lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Record originals, persist them, then set manual mode on mapped headers."""
        if self.started:
            return
        if not self.active:
            self.started = True
            return
        # A state file left by a killed run holds the true firmware originals. Put
        # those back first, so the values read below are not our own manual mode.
        if self.state_file.exists():
            try:
                fallbacks = restore_from_state_file(self.state_file)
            except StateFileError as exc:
                raise BackendError(f"stale state file blocks start: {exc}") from exc
            if fallbacks:
                raise BackendError(
                    "stale state file could not be fully restored for "
                    + ", ".join(sorted(fallbacks))
                )
        self.started = True
        try:
            for header_id in sorted(self.mapped):
                value = int(_read_text(_enable_path(self.headers[header_id])))
                self.originals[header_id] = value
            self._save_state()
            for header_id in sorted(self.originals):
                _write_int(_enable_path(self.headers[header_id]), MANUAL)
                self.expected[header_id] = MANUAL
        except (OSError, ValueError) as exc:
            self.restore()
            raise BackendError(f"cannot take control of fans: {exc}") from exc

    def restore(self) -> None:
        """Write full speed, then restore every recorded pwmN_enable.

        A header whose original mode was manual (1) is left at full speed, not at the
        last low duty. A header whose mode write fails stays at full speed too.
        """
        self.restore_failures = []
        with _signals_deferred():
            self._restore_all()
        self.started = False

    def _restore_all(self) -> None:
        for header_id, original in self.originals.items():
            pwm = self.headers[header_id]
            # Full speed goes in first. If the original mode is manual (1) the fan keeps
            # this duty after the restore, so it must never be the last low value.
            full_speed_written = True
            try:
                _write_int(pwm, FULL_PWM)
            except OSError as exc:
                full_speed_written = False
                log.error("full speed write to %s failed: %s", header_id, exc)
            try:
                _write_int(_enable_path(pwm), original)
            except OSError as exc:
                log.error("restore of %s failed (%s); fan left at full speed", header_id, exc)
                self.restore_failures.append(header_id)
            else:
                if not full_speed_written:
                    log.error("%s restored to mode %s without a full speed write", header_id, original)
        self.expected = {}
        if not self.restore_failures:
            self.originals = {}
            try:
                self.state_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.error("cannot remove state file: %s", exc)

    def __enter__(self) -> "SysfsBackend":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.restore()

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "headers": {h: self.headers[h] for h in self.originals},
            "originals": self.originals,
        }
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)

    # Backend interface -----------------------------------------------------------

    def read_inputs(self) -> Mapping[str, Reading]:
        """Return a reading per input; an unreadable input has value None."""
        now = self.clock()
        result: dict[str, Reading] = {}
        for name, path in self.inputs.items():
            try:
                result[name] = Reading(float(_read_text(path)) / self.input_scale, now)
            except (OSError, ValueError):
                result[name] = Reading(None, None)
        return result

    def read_rpm(self, header_id: str) -> float | None:
        path = self.headers.get(header_id)
        if path is None:
            return None
        try:
            return float(_read_text(_fan_path(path)))
        except (OSError, ValueError):
            return None

    def write_duty(self, header_id: str, duty: float) -> None:
        if not self._controlled(header_id):
            return
        _write_int(self.headers[header_id], duty_to_pwm(duty))

    def owns(self, header_id: str) -> bool:
        """False when pwmN_enable no longer holds the value this backend last set.

        Something else (a fan utility, firmware, another script) wrote the mode, so this
        service no longer knows what the fan is doing. An unreadable file counts as lost.
        """
        expected = self.expected.get(header_id)
        if expected is None or not self._controlled(header_id):
            return True
        try:
            return int(_read_text(_enable_path(self.headers[header_id]))) == expected
        except (OSError, ValueError):
            return False

    def release(self, header_id: str) -> None:
        """Hand one header back to its original mode, or full speed if that fails."""
        if not self._controlled(header_id):
            return
        pwm = self.headers[header_id]
        try:
            _write_int(_enable_path(pwm), self.originals[header_id])
            self.expected[header_id] = self.originals[header_id]
        except OSError as exc:
            log.error("release of %s failed (%s); writing full speed", header_id, exc)
            try:
                _write_int(pwm, FULL_PWM)
            except OSError as exc2:
                log.error("full speed write to %s failed: %s", header_id, exc2)


@contextlib.contextmanager
def _signals_deferred() -> Iterator[None]:
    """Ignore SIGTERM and SIGINT while restoring, so a second signal cannot cut it short."""
    saved: list[tuple[int, object]] = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            saved.append((sig, signal.signal(sig, signal.SIG_IGN)))
    except ValueError:
        pass  # Not the main thread: handlers cannot be changed, and none were installed here.
    try:
        yield
    finally:
        for sig, old in saved:
            signal.signal(sig, old)  # type: ignore[arg-type]


def install_signal_handlers() -> None:
    """Turn SIGTERM and SIGINT into SystemExit so the context manager restores."""

    def handler(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _valid_pwm_path(path: object) -> bool:
    """A state file may only name files called pwmN, so it cannot redirect writes."""
    if not isinstance(path, str) or not path:
        return False
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("pwm") and name[3:].isdigit()


def restore_from_state_file(state_file: str | Path) -> list[str]:
    """Restore originals saved by a killed service. Returns header ids that fell back.

    The state file is trusted to be root-only, since it names files that get written. On
    POSIX a file writable by group or others is refused, and every listed path must be a
    pwmN file. A missing file means nothing to restore and returns an empty list. An
    unreadable, malformed or untrusted file raises StateFileError, so the caller knows
    fans may still be in manual mode.
    """
    path = Path(state_file)
    if not path.exists():
        return []
    try:
        if os.name == "posix" and path.stat().st_mode & 0o022:
            raise StateFileError(f"{path} is writable by group or others")
        data = json.loads(path.read_text(encoding="utf-8"))
        headers = {str(h): p for h, p in data["headers"].items()}
        originals = {str(h): int(v) for h, v in data["originals"].items()}
    except StateFileError:
        log.error("state file %s is not trusted", path)
        raise
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        log.error("cannot read state file %s: %s", path, exc)
        raise StateFileError(f"cannot read {path}: {exc}") from exc
    if not all(_valid_pwm_path(p) for p in headers.values()) or set(originals) - set(headers):
        log.error("state file %s names paths that are not pwm files", path)
        raise StateFileError(f"{path} names paths that are not pwm files")
    backend = SysfsBackend(headers, {}, headers.keys(), path, active=True)
    backend.originals = originals
    backend.restore()
    return backend.restore_failures
