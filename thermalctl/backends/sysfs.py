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
audit = logging.getLogger("thermalctl.audit")

MANUAL = 1
FULL_PWM = 255
# The pwmN_enable value written when a state file cannot say what the original was. 5 is
# the value MediaIn-SVR reports for the chip's own control (UNVERIFIED.md). Whether a chip
# in that mode can hold a stale low duty is not measured, so every path that writes it
# writes full speed first where it can.
FIRMWARE_MODE = 5


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
    # No O_CREAT: a path that does not exist (for example an unresolved chip reference)
    # must fail, never create a stray file that looks like a successful write.
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
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
        clock: Callable[[], float] = time.monotonic,
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
        # Per input: the last value read and the clock time it last differed from the one
        # before. A reading is stamped with that time, not with the time of the read.
        self._changed: dict[str, tuple[float, float]] = {}

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
                # A truncated or corrupt file names no usable originals, and refusing to
                # start would leave every fan in manual mode at its last low duty. Put each
                # mapped header back under firmware control, set the file aside, and go on.
                self._recover_from_bad_state_file(exc)
                fallbacks = []
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

    def _recover_from_bad_state_file(self, exc: StateFileError) -> None:
        """Hand each mapped header still in manual mode to firmware control.

        A header in manual mode gets full speed first, so that if the mode write fails the
        fan is not left at a stale low duty, then firmware mode. A header whose mode cannot
        be read is treated the same way. A header already in another mode is under chip
        control, possibly in its true original mode, so it is left alone and start records
        that mode as the original. The bad file is renamed to keep it for diagnosis, under
        a fresh name when an earlier one is already kept, so the next start does not read
        it again. Every mode change goes to the audit log with its old and new value.
        """
        log.error("state file unusable (%s); restoring manual mapped headers to firmware mode", exc)
        for header_id in sorted(self.mapped):
            pwm = self.headers[header_id]
            try:
                old: int | None = int(_read_text(_enable_path(pwm)))
            except (OSError, ValueError):
                old = None
            if old is not None and old != MANUAL:
                audit.info("header %s left in mode %s: not in manual mode", header_id, old)
                continue
            full_speed = False
            if old is None or old == MANUAL:
                try:
                    _write_int(pwm, FULL_PWM)
                    full_speed = True
                except OSError as err:
                    log.error("full speed write to %s failed: %s", header_id, err)
            try:
                _write_int(_enable_path(pwm), FIRMWARE_MODE)
            except OSError as err:
                audit.error(
                    "header %s mode %s to %s failed: %s; %s",
                    header_id, "unreadable" if old is None else old, FIRMWARE_MODE, err,
                    "fan left at full speed" if full_speed else "fan left as it was",
                )
            else:
                audit.warning(
                    "header %s mode %s to %s (state file unusable)",
                    header_id, "unreadable" if old is None else old, FIRMWARE_MODE,
                )
        bad = self.state_file.with_name(self.state_file.name + ".bad")
        n = 1
        while bad.exists():
            bad = self.state_file.with_name(f"{self.state_file.name}.bad.{n}")
            n += 1
        try:
            os.replace(self.state_file, bad)
        except OSError as err:
            log.error("cannot set aside state file %s: %s", self.state_file, err)

    def recover_from_bad_state_file(self, exc: StateFileError) -> None:
        """Public entry for the restore command, which has no running service.

        Not gated on the active flag: the config may have been switched away from active
        after a run that left fans in manual mode, and restore must still make them safe.
        """
        self._recover_from_bad_state_file(exc)

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
            # A header already back in a firmware mode (for example after the mapping test
            # released it) is controlled by the chip, which refuses pwm writes with EBUSY on
            # nct6775. There is nothing to make safe, so skip the write instead of logging a
            # false error. Only a header still in manual mode, or one whose mode cannot be
            # read, gets the full speed write.
            try:
                current = int(_read_text(_enable_path(pwm)))
            except (OSError, ValueError):
                current = MANUAL
            if current == MANUAL:
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
                    if original == 1:
                        # A manual original keeps whatever duty is in pwmN, which would be the
                        # last low value. Try full speed once more; if that also fails, keep the
                        # state file so the restore helper retries instead of reporting success.
                        try:
                            _write_int(pwm, FULL_PWM)
                        except OSError as exc:
                            log.error("%s left in manual mode at its last duty: %s", header_id, exc)
                            self.restore_failures.append(header_id)
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
        """Return a reading per input; an unreadable input has value None.

        A reading is stamped with the time its value last changed, not the time it was read.
        A sensor whose chip has stopped updating keeps returning the same number, and a
        stamp of the read time would make that look fresh for ever. So a value that has not
        changed for stale_after_s counts as stale (the frozen-sensor rule). A sensor that
        legitimately holds one value for longer than stale_after_s is therefore treated as
        failed, which is the safe direction: pick a stale_after_s longer than that.
        """
        now = self.clock()
        result: dict[str, Reading] = {}
        for name, path in self.inputs.items():
            try:
                value = float(_read_text(path)) / self.input_scale
            except (OSError, ValueError):
                self._changed.pop(name, None)
                result[name] = Reading(None, None)
                continue
            seen = self._changed.get(name)
            # NaN never equals itself, so it counts as a change; the safety check rejects it.
            if seen is None or seen[0] != value:
                seen = (value, now)
                self._changed[name] = seen
            result[name] = Reading(value, seen[1])
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

    def holds(self, header_id: str, duty: float) -> bool:
        """True when pwmN still holds the value a write of duty would make.

        One read. An unreadable or unparsable file counts as not holding, so the caller
        writes again. A header this backend does not control has nothing to check.
        """
        if not self._controlled(header_id):
            return True
        try:
            return int(_read_text(self.headers[header_id])) == duty_to_pwm(duty)
        except (OSError, ValueError):
            return False

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

    def retake(self, header_id: str) -> None:
        """Write manual mode again after a foreign change, so duty writes take effect."""
        if not self._controlled(header_id):
            return
        previous = self.expected.get(header_id)
        _write_int(_enable_path(self.headers[header_id]), MANUAL)
        self.expected[header_id] = MANUAL
        if previous != MANUAL:
            audit.warning("header %s mode %s to %s (retaken)", header_id, previous, MANUAL)

    def release(self, header_id: str) -> None:
        """Hand one header back to its original mode, or full speed if that fails."""
        if not self._controlled(header_id):
            return
        pwm = self.headers[header_id]
        target = self.originals[header_id]
        if target == MANUAL:
            # A manual original keeps the stale duty, so full speed goes in first. If that
            # write fails, firmware control is the only state left that cannot hold it low.
            try:
                _write_int(pwm, FULL_PWM)
            except OSError as exc:
                log.error("full speed write to %s failed: %s; trying firmware mode", header_id, exc)
                target = FIRMWARE_MODE
        previous = self.expected.get(header_id)
        try:
            _write_int(_enable_path(pwm), target)
            self.expected[header_id] = target
            if previous != target:
                audit.warning("header %s mode %s to %s (released)", header_id, previous, target)
        except OSError as exc:
            log.error("release of %s failed (%s); writing full speed", header_id, exc)
            audit.error("header %s release to mode %s failed: %s", header_id, target, exc)
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
