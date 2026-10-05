"""The control loop: read, compute, protect, write, report.

One cycle reads inputs through a backend, computes a duty per header from its zones,
runs the fail-safe state machine and smoothing, writes only in active mode to mapped
headers, and publishes an atomic status file. Any exception inside a cycle forces
fail-safe on every header and the loop carries on, so one bad read can never leave a
fan slower than it should be or stop the service.
"""

from __future__ import annotations

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
from .config import Config, ConfigError, Header, load_effective
from .curves import zone_duty
from .hwmon import HwmonError
from .safety import FAILSAFE, LOAD_WARMING_UP, HeaderSafety, Reading, failsafe_duty
from .smoothing import Ema, OutputShaper, apply_floor

DEFAULT_STATUS_PATH = "/run/thermalctl/status.json"
CYCLE_ERROR = "cycle_error"
EXTERNAL_CHANGE = "external_change"

audit = logging.getLogger("thermalctl.audit")
log = logging.getLogger("thermalctl")


def write_status_atomic(path: str | Path, document: dict) -> None:
    """Write JSON to a uniquely named temp file beside the target, then rename over it.

    On any failure the temp file is removed and the previous target is left intact.
    """
    target = Path(path)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
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
        clock: Callable[[], float] = time.time,
        hold_s: float = 30.0,
        ema_alpha: float = 0.5,
        hysteresis: float = 2.0,
        ramp_down_per_s: float = 2.0,
        failsafe_firmware: bool = False,
        config_transform: Callable[[Config], Config] | None = None,
        overrides_path: str | Path | None = None,
    ) -> None:
        self.backend = backend
        self.status_path = Path(status_path)
        self.clock = clock
        self.hold_s = hold_s
        self.ema_alpha = ema_alpha
        self.hysteresis = hysteresis
        self.ramp_down_per_s = ramp_down_per_s
        self.failsafe_firmware = failsafe_firmware
        # Applied to every reloaded config, for example to resolve chip names to paths.
        self.config_transform = config_transform
        # The overrides file merged over every reloaded config; None uses the default path.
        self.overrides_path = overrides_path
        self.config_valid = True
        self.config = config
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

    # -- configuration -------------------------------------------------------------

    def _enabled(self, header: Header) -> bool:
        return self.config.mode == "active" and header.mapped

    def _build(self, config: Config) -> None:
        old = self.safety
        self.safety = {}
        for header in config.headers:
            machine = HeaderSafety(header, self.hold_s, config.mode == "active")
            prior = old.get(header.id)
            if prior is not None and prior.state == FAILSAFE:
                # Never leave failsafe just because the config was reloaded.
                machine.state = FAILSAFE
                machine.reasons = prior.reasons
                machine.last_change = prior.last_change
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
            if header.id in self.shapers:
                self.shapers[header.id].reset(100.0)

    def _restart_required(self, old: Config, new: Config) -> list[str]:
        """Changes the running backend cannot follow, which need a restart.

        The backend recorded the original pwmN_enable of the headers that were mapped at
        start, and in dry run it recorded nothing at all. Starting to drive a header it
        never took over would write to a fan with no recorded mode to restore.
        """
        problems: list[str] = []
        if old.mode == "dry_run" and new.mode == "active":
            problems.append("mode dry_run->active")
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

    def reload(self, path: str | Path) -> bool:
        """Load a config file. An invalid file keeps failsafe and the old config unused.

        A change that needs a restart (dry run to active, or a newly mapped header or a
        changed mapped path) is refused: the old config stays in force and nothing else
        changes. Stopping control of a header is always allowed.
        """
        try:
            new, _ = load_effective(path, self.overrides_path)
            if self.config_transform is not None:
                new = self.config_transform(new)
        except (ConfigError, HwmonError) as exc:
            audit.error("config reload rejected, failsafe stays in force: %s", exc)
            self.config_valid = False
            return False
        blocked = self._restart_required(self.config, new)
        if blocked:
            audit.error(
                "config reload refused, restart the service to apply: %s", "; ".join(blocked)
            )
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
        return True

    # -- one cycle ----------------------------------------------------------------

    def _header_duty(self, header: Header, readings: dict[str, Reading], dt: float) -> float:
        warming = False
        zones = {z.id: z for z in self.config.zones}
        target = 0.0
        for zid in header.zones:
            zone = zones[zid]
            temp = self.emas[(zid, zone.temperature_input)].update(
                readings[zone.temperature_input].value
            )
            load = None
            if zone.load_input is not None and readings[zone.load_input].warming_up:
                warming = True
            elif zone.load_input is not None:
                load = self.emas[(zid, zone.load_input)].update(readings[zone.load_input].value)
            target = max(target, zone_duty(zone.temperature_curve, temp, zone.load_curve, load))
        self.notes[header.id] = [LOAD_WARMING_UP] if warming else []
        shaped = self.shapers[header.id].apply(target, dt)
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
                if duty == 0.0:
                    self.backend.release(header.id)
                else:
                    # Another tool may have switched the chip to an automatic mode, which
                    # can ignore pwm writes, so take manual mode back before full speed.
                    self.backend.retake(header.id)
                    self.backend.write_duty(header.id, duty)
            except Exception:
                log.exception("failsafe write failed for %s", header.id)

    def _force_failsafe_all(self, now: float, reason: str) -> None:
        for header in self.config.headers:
            machine = self.safety[header.id]
            machine.force_failsafe(now, reason)
            self._failsafe_header(header)

    def cycle(self) -> dict:
        """Run one cycle; never raises. Returns the status document."""
        now = self.clock()
        readings: dict[str, Reading] = {}
        rpms: dict[str, float | None] = {}
        previous = {hid: m.state for hid, m in self.safety.items()}
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
                lost = (
                    (f"{EXTERNAL_CHANGE}:{header.id}",)
                    if self._enabled(header) and not self.backend.owns(header.id)
                    else ()
                )
                self.safety[header.id].update(
                    now,
                    extra_causes=lost,
                    zones=[zones[z] for z in header.zones],
                    readings=readings,
                    rpm=rpms[header.id],
                    commanded=self.commanded.get(header.id),
                    config_valid=self.config_valid,
                )
            for header in self.config.headers:
                if self.safety[header.id].state == FAILSAFE:
                    self._failsafe_header(header)
                    continue
                if previous.get(header.id) == FAILSAFE and self._enabled(header):
                    # Leaving failsafe: release() may have handed the header to firmware.
                    self.backend.retake(header.id)
                duty = self._header_duty(header, readings, dt)
                self.duty[header.id] = duty
                self.commanded[header.id] = duty
                if self._enabled(header):
                    self.backend.write_duty(header.id, duty)
        except Exception as exc:
            log.exception("cycle failed, forcing failsafe on every header")
            self._force_failsafe_all(now, f"{CYCLE_ERROR}:{type(exc).__name__}")
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
        def val(name: str | None) -> float | None:
            reading = readings.get(name) if name else None
            return None if reading is None else _finite(reading.value)

        return {
            "version": __version__,
            "timestamp": now,
            "mode": self.config.mode,
            "config_valid": self.config_valid,
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
                    "duty": self.duty.get(h.id),
                    "rpm": _finite(rpms.get(h.id)),
                    "reasons": list(self.safety[h.id].reasons),
                    "notes": list(self.notes.get(h.id, [])),
                    "last_change": self.safety[h.id].last_change,
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
