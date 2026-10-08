"""The control loop: read, compute, protect, write, report.

One cycle reads inputs through a backend, computes a duty per header from its zones,
runs the fail-safe state machine and smoothing, writes only in active mode to mapped
headers, and publishes an atomic status file. Any exception inside a cycle forces
fail-safe on every header and the loop carries on, so one bad read can never leave a
fan slower than it should be or stop the service.
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .backend import Backend
from .backends.sysfs import duty_to_pwm
from .config import (
    DEFAULT_OVERRIDES_PATH, Config, ConfigError, Header, OverrideReport, apply_overrides,
    load_config,
)
from .curves import zone_duty
from .load import SUPPORTED_LOAD_INPUTS
from .safety import (
    FAILSAFE, FAILSAFE_WRITE_FAILED, LOAD_WARMING_UP, HeaderSafety, Reading, failsafe_duty,
)
from .smoothing import Ema, OutputShaper, apply_floor

DEFAULT_STATUS_PATH = "/run/thermalctl/status.json"
CYCLE_ERROR = "cycle_error"
EXTERNAL_CHANGE = "external_change"
# A duty that has not changed is written again after this many cycles, whatever the
# verification read says, so any drift the read cannot see is corrected. 60 cycles is two
# minutes at the default 2 second interval.
FORCE_REFRESH_CYCLES = 60
# Marks a header handed to firmware control in the applied-state table, which has no duty.
_RELEASED = -1

audit = logging.getLogger("thermalctl.audit")
log = logging.getLogger("thermalctl")


def strict_json_safe(value: object) -> object:
    """Copy a document with every non-finite float replaced by None.

    NaN and Infinity are not JSON. Python writes them as bare words that a strict parser
    such as the one in hostwatch rejects, which would turn one bad number into an unreadable
    status file. null says "no value" and every reader already handles it.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: strict_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict_json_safe(v) for v in value]
    return value


def status_temp_files(path: str | Path) -> list[Path]:
    """The temp files write_status_atomic may have left beside the status file."""
    target = Path(path)
    try:
        return sorted(
            p for p in target.parent.glob(glob.escape(target.name) + ".*.tmp") if p.is_file()
        )
    except OSError:
        return []


def remove_stale_status_temps(path: str | Path) -> list[Path]:
    """Delete temp files left by a writer that was killed between create and rename.

    Only call this while holding the ownership lock, so no live writer owns one of them.
    Returns the files removed. A file that cannot be removed is logged and skipped.
    """
    removed: list[Path] = []
    for temp in status_temp_files(path):
        try:
            temp.unlink()
        except OSError as exc:
            log.warning("cannot remove stale status temp file %s: %s", temp, exc)
        else:
            removed.append(temp)
            log.info("removed stale status temp file %s", temp)
    return removed


def write_status_atomic(path: str | Path, document: dict) -> None:
    """Write JSON to a uniquely named temp file beside the target, then rename over it.

    On any failure the temp file is removed and the previous target is left intact. The
    output is strict JSON: a non-finite number is written as null.
    """
    target = Path(path)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                strict_json_safe(document), handle, indent=2, sort_keys=True, allow_nan=False
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def describe_changes(old: Config | None, new: Config) -> list[str]:
    """List every audited difference between two configs as old and new values."""
    if old is None:
        return [f"mode=None->{new.mode}"] + [
            f"header {h.id} mapped=None->{h.mapped}" for h in new.headers
        ]
    out: list[str] = []
    if old.mode != new.mode:
        out.append(f"mode={old.mode}->{new.mode}")
    header_fields = ("mapped", "path", "min_duty", "min_rpm", "stall_window_s", "zones", "min_rpm_duty")
    zone_fields = (
        "temperature_input", "temperature_curve", "hard_max_temp_c",
        "stale_after_s", "load_input", "load_curve",
    )
    for kind, olds, news, fields in (
        ("header", old.headers, new.headers, header_fields),
        ("zone", old.zones, new.zones, zone_fields),
    ):
        a_map = {x.id: x for x in olds}
        b_map = {x.id: x for x in news}
        for item_id in sorted(a_map.keys() | b_map.keys()):
            a, b = a_map.get(item_id), b_map.get(item_id)
            if a is None or b is None:
                out.append(f"{kind} {item_id} present={a is not None}->{b is not None}")
                continue
            for field in fields:
                if getattr(a, field) != getattr(b, field):
                    out.append(
                        f"{kind} {item_id} {field}={getattr(a, field)}->{getattr(b, field)}"
                    )
    return out


def _finite(value: object) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value) if ok and math.isfinite(value) else None


class Controller:
    def __init__(
        self,
        config: Config,
        backend: Backend,
        *,
        status_path: str | Path = DEFAULT_STATUS_PATH,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        hold_s: float = 30.0,
        ema_alpha: float = 0.5,
        hysteresis: float = 2.0,
        ramp_down_per_s: float = 2.0,
        failsafe_firmware: bool = False,
        config_transform: Callable[[Config], Config] | None = None,
        overrides_path: str | Path | None = None,
        config_path: str | Path | None = None,
        overrides_report: OverrideReport | None = None,
        base_config: Config | None = None,
        refresh_cycles: int = FORCE_REFRESH_CYCLES,
    ) -> None:
        self.backend = backend
        # What was last written per header: the register value (or _RELEASED) and the cycles
        # since it was written. A duty is written only when it differs from this, when the
        # verification read disagrees, or when refresh_cycles have passed.
        self.refresh_cycles = max(1, int(refresh_cycles))
        self._applied: dict[str, tuple[int, int]] = {}
        # pwm_enable ownership as read this cycle, so one read serves the safety check and
        # the failsafe verification.
        self._owned: dict[str, bool] = {}
        # Headers whose failsafe write failed and was already audited; cleared on success.
        self._write_failed: set[str] = set()
        self.status_path = Path(status_path)
        # Every timer (stall, low rpm, slow fan, hold, staleness) runs on this clock, which
        # must be monotonic: a step of the wall clock (NTP, RTC-less boot, VM resume) must
        # neither stop a timer nor finish one early. Sensor readings must be stamped from
        # the same clock. The default is time.monotonic.
        self.clock = time.monotonic if clock is None else clock
        # Only the status file's timestamps use wall time, because hostwatch compares them
        # with its own wall clock. An injected clock with no wall clock serves both, which
        # keeps fake-clock tests simple.
        self.wall_clock = (
            wall_clock if wall_clock is not None else (time.time if clock is None else self.clock)
        )
        self.hold_s = hold_s
        self.ema_alpha = ema_alpha
        self.hysteresis = hysteresis
        self.ramp_down_per_s = ramp_down_per_s
        self.failsafe_firmware = failsafe_firmware
        # Applied to every reloaded config, for example to resolve chip names to paths.
        self.config_transform = config_transform
        # The overrides file merged over every reloaded config; None uses the default path.
        self.overrides_path = overrides_path
        # The main config file the running config came from. When set, cycle() reloads it on
        # a reload request (SIGHUP) or when the overrides file changes on disk.
        self.config_path = config_path
        self.reload_requested = False
        # The sensor inputs the backend was started to read. A reload cannot add to them.
        self._backend_inputs = {z.temperature_input for z in config.zones}
        # Why the last main config reload was rejected or refused; None when all is well.
        self.config_error: str | None = None
        report = overrides_report if overrides_report is not None else OverrideReport("")
        self.overrides_applied = report.applied
        self.overrides_mode = report.mode
        # Why the last overrides reload was rejected or ignored; None when all is well.
        self.overrides_error: str | None = report.ignored
        # The active override and when it ends, in wall clock epoch seconds. The expiry is an
        # absolute time, so it is compared with the wall clock, not the monotonic timer clock.
        self._expiry_tried: float | None = None
        self.override_active = report.active
        self.override_expires_at: float | None = report.expires_at if report.applied else None
        self._stamps = self._stat_files()
        self.config_valid = True
        self.config = config
        # The running config without the overrides merged in. An expired override is retired
        # by going back to this, so the revert never depends on the main config on disk being
        # acceptable to a reload (it may hold a change that needs a restart).
        self._base_config: Config | None = base_config
        if self._base_config is None:
            if not report.applied:
                self._base_config = config
            elif config_path is not None:
                self._base_config = self._load_base(config_path)
        self.safety: dict[str, HeaderSafety] = {}
        self.shapers: dict[str, OutputShaper] = {}
        self.emas: dict[tuple[str, str], Ema] = {}
        self.commanded: dict[str, float | None] = {}
        self.duty: dict[str, float] = {}
        self.notes: dict[str, list[str]] = {}
        self.last_time: float | None = None
        self._build(config)
        for line in describe_changes(None, config):
            audit.info("config loaded: %s", line)
        if report.expired:
            audit.warning(
                "override expired at %s before start: base config in use", report.expires_at
            )
        elif report.active:
            audit.warning("override active, expires_at=%s", report.expires_at)

    # -- configuration -------------------------------------------------------------

    def _enabled(self, header: Header) -> bool:
        return self.config.mode == "active" and header.mapped

    def _build(self, config: Config) -> None:
        old = self.safety
        self.safety = {}
        # A new config may change a floor or a mapping, so every header is written afresh.
        self._applied.clear()
        for header in config.headers:
            machine = HeaderSafety(header, self.hold_s, config.mode == "active")
            prior = old.get(header.id)
            if prior is not None:
                # Never leave failsafe, and never restart a stall timer, just because the
                # config was reloaded.
                machine.adopt(prior)
            self.safety[header.id] = machine
            self.shapers.setdefault(
                header.id, OutputShaper(self.hysteresis, self.ramp_down_per_s)
            )
            self.commanded.setdefault(header.id, None)
        for zone in config.zones:
            for name in (zone.temperature_input, zone.load_input):
                if name is not None:
                    self.emas.setdefault((zone.id, name), Ema(self.ema_alpha))

    def _release_dropped(self, old: Config, new: Config) -> None:
        """Hand back every header this controller was driving that the new config will not.

        A header that was enabled under the old config but is not under the new one (dry
        run, unmapped or removed) would otherwise stay in manual PWM at its last duty.
        """
        new_headers = {h.id: h for h in new.headers}
        for header in old.headers:
            if not (old.mode == "active" and header.mapped):
                continue
            kept = new_headers.get(header.id)
            if kept is not None and new.mode == "active" and kept.mapped:
                continue
            duty = failsafe_duty(self.failsafe_firmware)
            audit.warning(
                "header %s no longer controlled, driving to %s",
                header.id, "firmware control" if duty == 0.0 else f"{duty} percent",
            )
            try:
                if duty == 0.0:
                    self.backend.release(header.id)
                else:
                    self.backend.write_duty(header.id, duty)
            except Exception:
                log.exception("release write failed for %s", header.id)
            self.commanded[header.id] = duty or None
            self.duty[header.id] = duty
            self._applied.pop(header.id, None)
            if header.id in self.shapers:
                self.shapers[header.id].reset(100.0)

    def _load_base(self, path: str | Path) -> Config | None:
        """The main config as a reload would see it, without overrides; None when unusable."""
        try:
            base = load_config(path)
            return self.config_transform(base) if self.config_transform is not None else base
        except Exception:
            log.exception("cannot load the base config")
            return None

    def _revert_override_in_place(self) -> bool:
        """Drop an expired override by returning to the running base config.

        Used when a reload could not retire the override, for example because the edited
        main config needs a restart and so is refused. The lowered floors must not outlast
        expires_at, so this does not consult the main config file when the base is known.
        """
        base = self._base_config
        from_disk = False
        if base is None and self.config_path is not None:
            base = self._load_base(self.config_path)
            from_disk = True
        if base is None:
            audit.error("override expired but the base config is unknown, floors stay lowered")
            return False
        if from_disk:
            # A config read fresh from disk is held to the same rule as a reload: a change the
            # running backend cannot follow is never applied as a side effect of expiry.
            blocked = self._restart_required(self.config, base)
            if blocked:
                audit.error(
                    "override expired but the base config on disk needs a restart (%s), "
                    "floors stay lowered",
                    "; ".join(blocked),
                )
                return False
        audit.warning(
            "override ended (expires_at=%s expired=True): base config in use",
            self.override_expires_at,
        )
        for line in describe_changes(self.config, base):
            audit.warning("config change: %s", line)
        if base != self.config:
            self._release_dropped(self.config, base)
            self.config = base
            self._build(base)
        self.override_active = False
        self.override_expires_at = None
        self.overrides_applied = False
        self.overrides_mode = None
        return True

    def _restart_required(self, old: Config, new: Config) -> list[str]:
        """Changes the running backend cannot follow, which need a restart.

        The backend recorded the original pwmN_enable of the headers that were mapped at
        start, and in dry run it recorded nothing at all. Starting to drive a header it
        never took over would write to a fan with no recorded mode to restore.
        """
        problems: list[str] = []
        if old.mode == "dry_run" and new.mode == "active":
            problems.append("mode dry_run->active")
        # The backend reads only the temperature inputs it was started with, so a zone that
        # moves to another sensor would starve and sit in failsafe for good.
        for zone in new.zones:
            if zone.temperature_input not in self._backend_inputs:
                problems.append(
                    f"zone {zone.id} temperature_input {zone.temperature_input} is not read "
                    "by the running backend"
                )
            if zone.load_input is not None and zone.load_input not in SUPPORTED_LOAD_INPUTS:
                problems.append(f"zone {zone.id} load_input {zone.load_input} is not supported")
        old_headers = {h.id: h for h in old.headers}
        for header in new.headers:
            if not header.mapped:
                continue
            before = old_headers.get(header.id)
            if before is None or not before.mapped:
                problems.append(f"header {header.id} newly mapped")
            elif before.path != header.path:
                problems.append(f"header {header.id} path {before.path}->{header.path}")
        return problems

    def reload(self, path: str | Path, *, use_overrides: bool = True) -> bool:
        """Load a config file. An invalid file keeps failsafe and the old config unused.

        A change that needs a restart (dry run to active, or a newly mapped header or a
        changed mapped path) is refused: the old config stays in force and nothing else
        changes. Stopping control of a header is always allowed. With use_overrides false the
        overrides file is skipped, which reverts an expired override even when its file can
        no longer be read.
        """
        try:
            new = load_config(path)
            if self.config_transform is not None:
                new = self.config_transform(new)
        except Exception as exc:
            # Not only ConfigError: a file that is not UTF-8, a number too large to parse or
            # a failed chip lookup is just as unusable, and must never escape as a crash or
            # be mistaken for a valid file.
            self.config_error = f"{type(exc).__name__}: {exc}"
            audit.error("config reload rejected, failsafe stays in force: %s", self.config_error)
            self.config_valid = False
            return False
        base = new
        # A bad overrides file is not a bad config: the previous effective config stays in
        # force, nothing goes to failsafe, and the reason is published in the status file.
        try:
            if use_overrides:
                new, report = apply_overrides(new, self.overrides_path, self.wall_clock())
            else:
                report = OverrideReport(str(self.overrides_path or DEFAULT_OVERRIDES_PATH))
        except Exception as exc:
            self.overrides_error = str(exc) if isinstance(exc, ConfigError) else (
                f"{type(exc).__name__}: {exc}"
            )
            audit.error("overrides rejected, previous effective config kept: %s", exc)
            return False
        if new.mode != self.config.mode and (
            report.mode is not None or self.overrides_mode is not None
        ):
            self.overrides_error = (
                f"mode {self.config.mode}->{new.mode} from overrides needs a restart"
            )
            audit.error("overrides reload refused, %s", self.overrides_error)
            return False
        blocked = self._restart_required(self.config, new)
        if blocked:
            self.config_error = "restart required: " + "; ".join(blocked)
            audit.error("config reload refused, restart the service to apply: %s", "; ".join(blocked))
            return False
        if not self.config_valid:
            audit.info("config valid again: invalid_config cleared, hold period applies")
        for line in describe_changes(self.config, new):
            audit.warning("config change: %s", line)
        if new != self.config:
            self._release_dropped(self.config, new)
            self.config = new
            self._build(new)
        self.config_valid = True
        self.config_error = None
        self._base_config = base
        self.overrides_applied = report.applied
        self.overrides_mode = report.mode
        self.overrides_error = report.ignored
        if self.override_active and not report.active:
            audit.warning(
                "override ended (expires_at=%s expired=%s): base config in use",
                self.override_expires_at, report.expired or not use_overrides,
            )
        self.override_active = report.active
        self.override_expires_at = report.expires_at if report.applied else None
        return True

    def _stat_files(self) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """Cheap fingerprints of the main config and the overrides file; None when missing."""
        out = []
        for path in (self.config_path, self.overrides_path or DEFAULT_OVERRIDES_PATH):
            try:
                st = os.stat(path)
                out.append((st.st_mtime_ns, st.st_size))
            except (OSError, TypeError, ValueError):
                out.append(None)
        return (out[0], out[1])

    def request_reload(self) -> None:
        """Ask for a reload at the start of the next cycle; safe to call from a signal handler."""
        self.reload_requested = True

    def _poll_reload(self) -> None:
        if self.config_path is None:
            return
        stamps = self._stat_files()
        expired = self._override_expired()
        if not self.reload_requested and not expired and stamps == self._stamps:
            return
        self.reload_requested = False
        self._stamps = stamps
        try:
            self.reload(self.config_path)
        except Exception:
            log.exception("reload failed")
        if self._override_expired():
            # Try the forced revert once per expiry; a refusal repeats every cycle otherwise.
            self._expiry_tried = self.override_expires_at
            # The reload could not retire the override (the file is unreadable, or the main
            # config was refused). The floor must not stay lowered past its end, so drop the
            # overrides file from the effective config altogether.
            try:
                self.reload(self.config_path, use_overrides=False)
            except Exception:
                log.exception("override expiry revert failed")
            # The main config may itself be refused (a change that needs a restart), which
            # blocks both reloads above. Then go back to the running base config directly.
            if self.override_active and self.override_expires_at is not None:
                try:
                    done = self._revert_override_in_place()
                except Exception:
                    log.exception("in-place override revert failed")
                    done = False
                if not done:
                    self._expiry_tried = None

    def _override_expired(self) -> bool:
        return (
            self.override_expires_at is not None
            and self.override_expires_at != self._expiry_tried
            and self.wall_clock() >= self.override_expires_at
        )

    # -- one cycle ----------------------------------------------------------------

    def _smooth_zones(
        self, readings: dict[str, Reading]
    ) -> dict[str, tuple[float, float | None, bool]]:
        """Advance each zone's smoothing once for this cycle.

        Several headers can share a zone. Each zone is updated here once, and every header
        reads the result, so the average moves one step per cycle and not one per header.
        Only zones used by a header that is not in failsafe are advanced.
        """
        used = {
            zid
            for header in self.config.headers
            if self.safety[header.id].state != FAILSAFE
            for zid in header.zones
        }
        smoothed: dict[str, tuple[float, float | None, bool]] = {}
        for zone in self.config.zones:
            if zone.id not in used:
                continue
            temp = self.emas[(zone.id, zone.temperature_input)].update(
                readings[zone.temperature_input].value
            )
            load = None
            warming = False
            if zone.load_input is not None and readings[zone.load_input].warming_up:
                warming = True
            elif zone.load_input is not None:
                load = self.emas[(zone.id, zone.load_input)].update(
                    readings[zone.load_input].value
                )
            smoothed[zone.id] = (temp, load, warming)
        return smoothed

    def _header_duty(
        self,
        header: Header,
        smoothed: dict[str, tuple[float, float | None, bool]],
        dt: float,
    ) -> float:
        warming = False
        zones = {z.id: z for z in self.config.zones}
        target = 0.0
        for zid in header.zones:
            zone = zones[zid]
            temp, load, zone_warming = smoothed[zid]
            warming = warming or zone_warming
            target = max(target, zone_duty(zone.temperature_curve, temp, zone.load_curve, load))
        self.notes[header.id] = [LOAD_WARMING_UP] if warming else []
        # The floor goes in before the shaper so that lowering it ramps down at the normal
        # rate instead of dropping in one step, and raising it still takes effect at once.
        shaped = self.shapers[header.id].apply(max(target, header.min_duty), dt)
        return apply_floor(shaped, header.min_duty)

    def _failsafe_header(self, header: Header) -> None:
        """Drive one header to full speed or firmware and forget smoothing history."""
        duty = failsafe_duty(self.failsafe_firmware)
        # After recovery the duty ramps down from full speed, never up from a stale value.
        self.shapers[header.id].reset(100.0)
        for zone in self.config.zones:
            if zone.id not in header.zones:
                continue
            for name in (zone.temperature_input, zone.load_input):
                if name is not None and (zone.id, name) in self.emas:
                    self.emas[(zone.id, name)].reset()
        self.duty[header.id] = duty
        self.notes[header.id] = []
        self.commanded[header.id] = duty or None
        if self._enabled(header):
            try:
                self._hold_failsafe(header, duty)
                self._write_failed.discard(header.id)
            except Exception:
                log.exception("failsafe write failed for %s", header.id)
                self._applied.pop(header.id, None)
                self._hand_to_firmware(header)

    def _owned_now(self, header: Header) -> bool:
        """pwm_enable ownership, reusing this cycle's read when the safety check made one."""
        owned = self._owned.get(header.id)
        if owned is None:
            try:
                owned = self.backend.owns(header.id)
            except Exception:
                owned = False
        return owned

    def _holds(self, header: Header, duty: float) -> bool:
        try:
            return bool(self.backend.holds(header.id, duty))
        except Exception:
            return False

    def _released(self, header: Header) -> bool:
        try:
            return bool(self.backend.released(header.id))
        except Exception:
            return False

    def _hold_failsafe(self, header: Header, duty: float) -> None:
        """Write the failsafe state once, then only verify it with reads.

        The write is manual mode and full speed (or the release to firmware). On later
        cycles the mode read and the duty read confirm it, and either one differing from
        what was written triggers the write again in the same cycle, so an external change
        is corrected within one cycle. The write is also repeated every refresh_cycles.
        """
        target = duty_to_pwm(duty) if duty != 0.0 else _RELEASED
        previous = self._applied.get(header.id)
        if previous is not None and previous[0] == target and previous[1] + 1 < self.refresh_cycles:
            # A release is verified by the backend: it fails when the mode write failed and,
            # for a header released into manual mode, when pwmN no longer holds full speed.
            verified = self._owned_now(header) and (
                self._released(header) if target == _RELEASED else self._holds(header, duty)
            )
            if verified:
                self._applied[header.id] = (target, previous[1] + 1)
                return
        if duty == 0.0:
            self.backend.release(header.id)
        else:
            # Another tool may have switched the chip to an automatic mode, which can
            # ignore pwm writes, so take manual mode back before full speed.
            self.backend.retake(header.id)
            self.backend.write_duty(header.id, duty)
        self._applied[header.id] = (target, 0)

    def _write_active(self, header: Header, duty: float) -> None:
        """Write a computed duty only when the register would change or may have drifted."""
        target = duty_to_pwm(duty)
        previous = self._applied.get(header.id)
        if previous is not None and previous[0] == target and previous[1] + 1 < self.refresh_cycles:
            if self._holds(header, duty):
                self._applied[header.id] = (target, previous[1] + 1)
                return
        self.backend.write_duty(header.id, duty)
        self._applied[header.id] = (target, 0)

    def _hand_to_firmware(self, header: Header) -> None:
        """The full speed write failed, so give the header back to the chip and say so.

        A header left in manual mode keeps its last duty, which may be low. Firmware
        control is the only other state that cannot leave the fan slow, so try it, make the
        status file and the audit log report that full speed was not written, and never
        claim a duty the fan does not have.
        """
        if header.id not in self._write_failed:
            # One line per episode, not per cycle, so a stuck header cannot flood the log.
            self._write_failed.add(header.id)
            audit.error(
                "header %s failsafe write failed (duty was %s): handing the header back to "
                "the chip; the mode written and any failure to write it are audited by the "
                "backend",
                header.id, self.duty.get(header.id),
            )
        try:
            self.backend.release(header.id)
        except Exception:
            log.exception("release after failed failsafe write also failed for %s", header.id)
        self.duty[header.id] = 0.0
        self.commanded[header.id] = None
        self.notes[header.id] = [FAILSAFE_WRITE_FAILED]

    def _force_failsafe_all(self, now: float, reason: str) -> None:
        for header in self.config.headers:
            machine = self.safety[header.id]
            machine.force_failsafe(now, reason)
            self._failsafe_header(header)

    def cycle(self) -> dict:
        """Run one cycle; never raises. Returns the status document."""
        self._poll_reload()
        now = self.clock()
        readings: dict[str, Reading] = {}
        rpms: dict[str, float | None] = {}
        previous = {hid: m.state for hid, m in self.safety.items()}
        self._owned = {}
        try:
            readings = dict(self.backend.read_inputs())
            rpms = {h.id: self.backend.read_rpm(h.id) for h in self.config.headers}
            # Take the cycle time after the reads. Readings are stamped as they are taken,
            # so a time taken before them makes every fresh reading look a little in the
            # future, and the staleness check rightly treats a future timestamp as stale.
            now = self.clock()
            dt = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
            zones = {z.id: z for z in self.config.zones}
            for header in self.config.headers:
                lost: tuple[str, ...] = ()
                if self._enabled(header):
                    self._owned[header.id] = self.backend.owns(header.id)
                    if not self._owned[header.id]:
                        lost = (f"{EXTERNAL_CHANGE}:{header.id}",)
                self.safety[header.id].update(
                    now,
                    extra_causes=lost,
                    zones=[zones[z] for z in header.zones],
                    readings=readings,
                    rpm=rpms[header.id],
                    commanded=self.commanded.get(header.id),
                    config_valid=self.config_valid,
                )
            # Failsafe headers go first. They reset the smoothing of their zones, which must
            # happen before the healthy headers advance the same zones for this cycle.
            for header in self.config.headers:
                if self.safety[header.id].state == FAILSAFE:
                    self._failsafe_header(header)
            smoothed = self._smooth_zones(readings)
            for header in self.config.headers:
                if self.safety[header.id].state == FAILSAFE:
                    continue
                if previous.get(header.id) == FAILSAFE and self._enabled(header):
                    # Leaving failsafe: release() may have handed the header to firmware.
                    self.backend.retake(header.id)
                    # Whatever the failsafe left is not a duty this loop wrote.
                    self._applied.pop(header.id, None)
                duty = self._header_duty(header, smoothed, dt)
                self.duty[header.id] = duty
                self.commanded[header.id] = duty
                if self._enabled(header):
                    self._write_active(header, duty)
        except Exception as exc:
            log.exception("cycle failed, forcing failsafe on every header")
            self._force_failsafe_all(now, f"{CYCLE_ERROR}:{type(exc).__name__}")
        self._owned = {}
        self.last_time = now
        for hid, machine in self.safety.items():
            if previous.get(hid) != machine.state:
                audit.warning(
                    "header %s state %s->%s reasons=%s",
                    hid, previous.get(hid), machine.state, list(machine.reasons),
                )
        document = self._status(now, readings, rpms)
        try:
            write_status_atomic(self.status_path, document)
        except Exception:
            log.exception("status file write failed")
        return document

    def _status(self, now: float, readings: dict, rpms: dict) -> dict:
        wall_now = self.wall_clock()

        def wall(stamp: float | None) -> float | None:
            """A timer-clock instant as wall time, by its age, so the file never mixes clocks."""
            return None if stamp is None else wall_now - (now - stamp)

        def val(name: str | None) -> float | None:
            reading = readings.get(name) if name else None
            return None if reading is None else _finite(reading.value)

        return {
            "version": __version__,
            "timestamp": wall_now,
            "mode": self.config.mode,
            "config_valid": self.config_valid,
            "overrides_applied": self.overrides_applied,
            "overrides_error": self.overrides_error,
            "override_active": self.override_active,
            "override_expires_at": self.override_expires_at,
            "overrides_mode": self.overrides_mode,
            "config_error": self.config_error,
            "zones": {
                z.id: {
                    "temperature": val(z.temperature_input),
                    "load": val(z.load_input),
                    "temperature_curve": [list(p) for p in z.temperature_curve],
                    "load_curve": None
                    if z.load_curve is None
                    else [list(p) for p in z.load_curve],
                }
                for z in self.config.zones
            },
            "headers": {
                h.id: {
                    "state": self.safety[h.id].state,
                    "mapped": h.mapped,
                    "min_duty": h.min_duty,
                    "duty": self.duty.get(h.id),
                    "rpm": _finite(rpms.get(h.id)),
                    "reasons": list(self.safety[h.id].reasons),
                    "notes": list(self.notes.get(h.id, [])),
                    "last_change": wall(self.safety[h.id].last_change),
                    "zones": list(h.zones),
                }
                for h in self.config.headers
            },
        }

    # -- running and exit -----------------------------------------------------------

    def run(
        self,
        interval_s: float,
        should_stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            while not should_stop():
                self.cycle()
                sleep(interval_s)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Latch failsafe and hand mapped headers to full speed or firmware."""
        now = self.clock()
        for header in self.config.headers:
            machine = self.safety[header.id]
            machine.request_exit()
            machine.force_failsafe(now, "exiting")
            self._failsafe_header(header)
        try:
            write_status_atomic(self.status_path, self._status(now, {}, {}))
        except Exception:
            log.exception("status file write failed at exit")
