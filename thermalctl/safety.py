"""Per-header fail-safe state machine.

A header is in one of three states. dry_run and active are the two healthy states
(which one depends on the config mode and the header mapping). failsafe means full
speed, or firmware control when the config says so. Any doubt about an input or a fan
enters failsafe at once. Leaving needs every cause clear for the whole hold period, so
a flapping input cannot bounce the fan between speeds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .config import Header, Zone

DRY_RUN = "dry_run"
ACTIVE = "active"
FAILSAFE = "failsafe"

MISSING_INPUT = "missing_input"
INVALID_INPUT = "invalid_input"
STALE_INPUT = "stale_input"
OVER_TEMP = "over_temp"
STALL = "stall"
INVALID_CONFIG = "invalid_config"
EXITING = "exiting"


@dataclass(frozen=True)
class Reading:
    """A sensor value and the time it was taken, on the same clock as `now`."""

    value: float | None
    timestamp: float | None


def _number_ok(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _check_reading(
    name: str, reading: Reading | None, now: float, stale_after_s: float
) -> list[str]:
    if reading is None or reading.value is None or reading.timestamp is None:
        return [f"{MISSING_INPUT}:{name}"]
    if not _number_ok(reading.value) or not _number_ok(reading.timestamp):
        return [f"{INVALID_INPUT}:{name}"]
    age = now - reading.timestamp
    # A timestamp in the future means the clock or the reader is wrong, so it counts as stale.
    if age < 0 or age > stale_after_s:
        return [f"{STALE_INPUT}:{name}"]
    return []


def input_causes(
    zones: list[Zone], readings: Mapping[str, Reading], now: float
) -> list[str]:
    """Return fail-safe causes for the given zones' temperature and load inputs."""
    causes: list[str] = []
    for zone in zones:
        temp = readings.get(zone.temperature_input)
        found = _check_reading(zone.temperature_input, temp, now, zone.stale_after_s)
        causes += found
        if not found and temp.value > zone.hard_max_temp_c:
            causes.append(f"{OVER_TEMP}:{zone.id}")
        if zone.load_input is not None:
            causes += _check_reading(
                zone.load_input, readings.get(zone.load_input), now, zone.stale_after_s
            )
    return causes


class HeaderSafety:
    """Tracks state, reasons and the stall timer for one header."""

    def __init__(self, header: Header, hold_s: float, enabled: bool) -> None:
        if not (math.isfinite(hold_s) and hold_s > 0):
            raise ValueError("hold_s must be greater than 0")
        self.header = header
        self.hold_s = hold_s
        # Only an enabled, mapped header is ever driven; the rest stay in dry run.
        self.healthy_state = ACTIVE if enabled and header.mapped else DRY_RUN
        self.state = self.healthy_state
        self.reasons: tuple[str, ...] = ()
        self.last_change: float | None = None
        self._clear_since: float | None = None
        self._stall_since: float | None = None
        self._exiting = False

    def request_exit(self) -> None:
        """Latch failsafe; the controller is shutting down."""
        self._exiting = True

    def force_failsafe(self, now: float, reason: str) -> None:
        """Enter failsafe at once for a cause found outside the normal checks."""
        if self.state != FAILSAFE:
            self.state = FAILSAFE
            self.last_change = now
        self.reasons = (reason,)
        self._clear_since = None
        self._stall_since = None

    def _stall_causes(
        self, now: float, commanded: float | None, rpm: float | None
    ) -> list[str]:
        if not _number_ok(rpm) or rpm < 0:
            self._stall_since = None
            return [f"{INVALID_INPUT}:rpm:{self.header.id}"]
        if commanded is not None and commanded > self.header.min_duty and rpm == 0:
            if self._stall_since is None:
                self._stall_since = now
            if now - self._stall_since > self.header.stall_window_s:
                return [f"{STALL}:{self.header.id}"]
        else:
            self._stall_since = None
        return []

    def update(
        self,
        now: float,
        *,
        zones: list[Zone],
        readings: Mapping[str, Reading],
        rpm: float | None,
        commanded: float | None,
        config_valid: bool = True,
    ) -> str:
        """Evaluate one cycle and return the new state."""
        causes: list[str] = []
        if not config_valid:
            causes.append(INVALID_CONFIG)
        if self._exiting:
            causes.append(EXITING)
        causes += input_causes(zones, readings, now)
        causes += self._stall_causes(now, commanded, rpm)
        self._apply(now, causes)
        return self.state

    def _apply(self, now: float, causes: list[str]) -> None:
        if causes:
            self._clear_since = None
            if self.state != FAILSAFE:
                self.state = FAILSAFE
                self.last_change = now
            self.reasons = tuple(causes)
        elif self.state == FAILSAFE:
            if self._clear_since is None:
                self._clear_since = now
            if now - self._clear_since >= self.hold_s:
                self.state = self.healthy_state
                self.reasons = ()
                self.last_change = now
                self._clear_since = None
                self._stall_since = None


def failsafe_duty(firmware_mode: bool) -> float:
    """Duty for a header in failsafe: 100, or 0 meaning hand back to firmware."""
    return 0.0 if firmware_mode else 100.0
