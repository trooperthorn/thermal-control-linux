"""Load and validate the TOML configuration.

Every problem raises ConfigError. The controller treats a ConfigError as a reason to
enter fail-safe, so validation is strict and rejects anything ambiguous.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .curves import Point

MODES = ("dry_run", "active")
TEMP_RANGE = (-50.0, 150.0)
LOAD_RANGE = (0.0, 100.0)
DEFAULT_PLAUSIBLE_TEMP = (-20.0, 150.0)


class ConfigError(ValueError):
    """The configuration is invalid and must not be used."""


@dataclass(frozen=True)
class Zone:
    id: str
    temperature_input: str
    temperature_curve: tuple[Point, ...]
    hard_max_temp_c: float
    stale_after_s: float
    load_input: str | None = None
    load_curve: tuple[Point, ...] | None = None
    plausible_min_c: float = DEFAULT_PLAUSIBLE_TEMP[0]
    plausible_max_c: float = DEFAULT_PLAUSIBLE_TEMP[1]


@dataclass(frozen=True)
class Header:
    id: str
    path: str
    mapped: bool
    min_duty: float
    min_rpm: int
    stall_window_s: float
    zones: tuple[str, ...]
    min_rpm_duty: float = 50.0


@dataclass(frozen=True)
class Config:
    mode: str
    zones: tuple[Zone, ...]
    headers: tuple[Header, ...]


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    return float(value)


def _string(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key} must be a non-empty string")
    return value


def _duty(value: object, name: str) -> float:
    duty = _number(value, name)
    if not 0.0 <= duty <= 100.0:
        raise ConfigError(f"{name} must be between 0 and 100")
    return duty


def _curve(raw: object, where: str, x_range: tuple[float, float]) -> tuple[Point, ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ConfigError(f"{where} needs at least two points")
    points: list[Point] = []
    for i, item in enumerate(raw):
        name = f"{where} point {i + 1}"
        if not isinstance(item, list) or len(item) != 2:
            raise ConfigError(f"{name} must be a pair [input, duty]")
        x = _number(item[0], f"{name} input")
        if not x_range[0] <= x <= x_range[1]:
            raise ConfigError(
                f"{name} input {x:g} is outside {x_range[0]:g} to {x_range[1]:g}"
            )
        y = _duty(item[1], f"{name} duty")
        if points and x <= points[-1][0]:
            raise ConfigError(f"{where} points must be sorted by strictly increasing input")
        # A fan must never slow down as the input rises, so duty may only stay or climb.
        if points and y < points[-1][1]:
            raise ConfigError(
                f"{name} duty {y:g} is lower than the previous point; "
                "duty must not decrease as the input rises"
            )
        points.append((x, y))
    return tuple(points)


def _positive(table: dict, key: str, where: str) -> float:
    value = _number(table.get(key), f"{where}: {key}")
    if value <= 0:
        raise ConfigError(f"{where}: {key} must be greater than 0")
    return value


def _zone(table: object, index: int) -> Zone:
    if not isinstance(table, dict):
        raise ConfigError(f"zone {index + 1} must be a table")
    zid = _string(table, "id", f"zone {index + 1}")
    where = f"zone {zid}"
    load_input = table.get("load_input")
    load_raw = table.get("load_curve")
    if load_input is not None and (not isinstance(load_input, str) or not load_input):
        raise ConfigError(f"{where}: load_input must be a non-empty string")
    if (load_input is None) != (load_raw is None):
        raise ConfigError(f"{where}: load_input and load_curve must be set together")
    hard_max = _number(table.get("hard_max_temp_c"), f"{where}: hard_max_temp_c")
    if not TEMP_RANGE[0] <= hard_max <= TEMP_RANGE[1]:
        raise ConfigError(f"{where}: hard_max_temp_c is outside the allowed range")
    temperature_curve = _curve(
        table.get("temperature_curve"), f"{where} temperature_curve", TEMP_RANGE
    )
    plausible_min = _number(
        table.get("plausible_min_c", DEFAULT_PLAUSIBLE_TEMP[0]), f"{where}: plausible_min_c"
    )
    plausible_max = _number(
        table.get("plausible_max_c", DEFAULT_PLAUSIBLE_TEMP[1]), f"{where}: plausible_max_c"
    )
    if plausible_min >= plausible_max:
        raise ConfigError(f"{where}: plausible_min_c must be below plausible_max_c")
    if hard_max > plausible_max:
        raise ConfigError(f"{where}: hard_max_temp_c is above plausible_max_c")
    # The hottest allowed temperature must already mean full speed, so the curve has to
    # reach 100 percent at or below it; duty is non-decreasing, so one point is enough.
    if not any(x <= hard_max and y >= 100.0 for x, y in temperature_curve):
        raise ConfigError(
            f"{where}: temperature_curve must reach 100 percent at or below "
            f"hard_max_temp_c ({hard_max:g})"
        )
    return Zone(
        id=zid,
        temperature_input=_string(table, "temperature_input", where),
        temperature_curve=temperature_curve,
        hard_max_temp_c=hard_max,
        stale_after_s=_positive(table, "stale_after_s", where),
        load_input=load_input,
        load_curve=None
        if load_raw is None
        else _curve(load_raw, f"{where} load_curve", LOAD_RANGE),
        plausible_min_c=plausible_min,
        plausible_max_c=plausible_max,
    )


def _header(table: object, index: int, zone_ids: set[str]) -> Header:
    if not isinstance(table, dict):
        raise ConfigError(f"header {index + 1} must be a table")
    hid = _string(table, "id", f"header {index + 1}")
    where = f"header {hid}"
    mapped = table.get("mapped", False)
    if not isinstance(mapped, bool):
        raise ConfigError(f"{where}: mapped must be true or false")
    min_rpm = _number(table.get("min_rpm"), f"{where}: min_rpm")
    if min_rpm < 0 or min_rpm != int(min_rpm):
        raise ConfigError(f"{where}: min_rpm must be a whole number of 0 or more")
    zones = table.get("zones")
    if not isinstance(zones, list) or not zones or not all(isinstance(z, str) for z in zones):
        raise ConfigError(f"{where}: zones must be a non-empty list of zone ids")
    for z in zones:
        if z not in zone_ids:
            raise ConfigError(f"{where}: unknown zone {z}")
    return Header(
        id=hid,
        path=_string(table, "path", where),
        mapped=mapped,
        min_duty=_duty(table.get("min_duty"), f"{where}: min_duty"),
        min_rpm=int(min_rpm),
        stall_window_s=_positive(table, "stall_window_s", where),
        zones=tuple(zones),
        min_rpm_duty=_duty(table.get("min_rpm_duty", 50), f"{where}: min_rpm_duty"),
    )


def parse_config(data: dict) -> Config:
    """Validate an already parsed TOML document."""
    mode = data.get("mode", "dry_run")
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}")
    raw_zones = data.get("zones")
    raw_headers = data.get("headers")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ConfigError("at least one zone is required")
    if not isinstance(raw_headers, list) or not raw_headers:
        raise ConfigError("at least one header is required")
    zones = tuple(_zone(t, i) for i, t in enumerate(raw_zones))
    zone_ids = [z.id for z in zones]
    if len(set(zone_ids)) != len(zone_ids):
        raise ConfigError("zone ids must be unique")
    headers = tuple(_header(t, i, set(zone_ids)) for i, t in enumerate(raw_headers))
    header_ids = [h.id for h in headers]
    if len(set(header_ids)) != len(header_ids):
        raise ConfigError("header ids must be unique")
    return Config(mode=mode, zones=zones, headers=headers)


def load_config(path: str | Path) -> Config:
    """Read and validate a TOML file; any failure is a ConfigError."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from exc
    return parse_config(data)
