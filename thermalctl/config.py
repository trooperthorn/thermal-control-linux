"""Load and validate the TOML configuration.

Every problem raises ConfigError. The controller treats a ConfigError as a reason to
enter fail-safe, so validation is strict and rejects anything ambiguous.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import time
import tomllib
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .curves import Point

DEFAULT_OVERRIDES_PATH = "/etc/thermalctl/overrides.toml"
MODES = ("dry_run", "active")
TEMP_RANGE = (-50.0, 150.0)
LOAD_RANGE = (0.0, 100.0)
DEFAULT_PLAUSIBLE_TEMP = (-20.0, 150.0)
# 15 minutes. A 1 C sensor on an idle host can hold one reading for many minutes, and only a
# value unchanged for much longer is taken as a chip that stopped updating. See UNVERIFIED.md.
DEFAULT_FROZEN_AFTER_S = 900.0
# A chip reference such as nct6779:pwm2, resolved to the current hwmonN at start.
CHIP_REF = re.compile(r"^(?P<chip>[A-Za-z0-9_.-]+):(?P<file>(?:pwm\d+|temp\d+_input))$")


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
    # How long a value may stay exactly the same before the sensor counts as frozen. It is
    # separate from stale_after_s: a quiet host legitimately holds one reading for minutes.
    frozen_after_s: float = DEFAULT_FROZEN_AFTER_S


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
    # The lowest floor an override may set; None means the header's own min_duty.
    min_duty_limit: float | None = None


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
    stale_after = _positive(table, "stale_after_s", where)
    frozen_after = (
        _positive(table, "frozen_after_s", where)
        if "frozen_after_s" in table
        else max(DEFAULT_FROZEN_AFTER_S, stale_after)
    )
    if frozen_after < stale_after:
        raise ConfigError(f"{where}: frozen_after_s must not be below stale_after_s")
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
        stale_after_s=stale_after,
        load_input=load_input,
        load_curve=None
        if load_raw is None
        else _curve(load_raw, f"{where} load_curve", LOAD_RANGE),
        plausible_min_c=plausible_min,
        plausible_max_c=plausible_max,
        frozen_after_s=frozen_after,
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
    path = _string(table, "path", where)
    # A value with a colon and no separator is meant as a chip reference. A misspelled
    # one would be opened as a relative file and read as a dead fan, so reject it.
    if ":" in path and "/" not in path and "\\" not in path and not CHIP_REF.match(path):
        raise ConfigError(
            f"{where}: path must be a file path or a chip reference like nct6779:pwm2"
        )
    min_duty = _duty(table.get("min_duty"), f"{where}: min_duty")
    min_duty_limit = _duty(table.get("min_duty_limit", min_duty), f"{where}: min_duty_limit")
    if min_duty_limit > min_duty:
        raise ConfigError(f"{where}: min_duty_limit must not be above min_duty")
    return Header(
        id=hid,
        path=path,
        mapped=mapped,
        min_duty=min_duty,
        min_rpm=int(min_rpm),
        stall_window_s=_positive(table, "stall_window_s", where),
        zones=tuple(zones),
        min_rpm_duty=_duty(table.get("min_rpm_duty", 50), f"{where}: min_rpm_duty"),
        min_duty_limit=min_duty_limit,
    )


def check_unique_paths(headers: tuple[Header, ...]) -> None:
    """Refuse two headers that drive the same pwm file.

    The two would each write their own duty to one file, so the fan would follow whichever
    wrote last, which can be the lower one while the other header's zone is hot. Paths are
    compared after normalising separators and dot segments. The check runs again once chip
    references are resolved, because nct6779:pwm1 and the real path name one file.
    """
    owner: dict[str, str] = {}
    for header in headers:
        key = os.path.normpath(header.path.replace("\\", "/"))
        if key in owner:
            raise ConfigError(
                f"headers {owner[key]} and {header.id} use the same pwm file {header.path}; "
                "each header needs its own"
            )
        owner[key] = header.id


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
    check_unique_paths(headers)
    return Config(mode=mode, zones=zones, headers=headers)


def load_config(path: str | Path) -> Config:
    """Read and validate a TOML file; any failure is a ConfigError."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    except UnicodeDecodeError:
        raise
    except ValueError as exc:  # TOMLDecodeError, or an integer too long for Python to parse
        raise ConfigError(f"config is not valid TOML: {exc}") from exc
    return parse_config(data)


@dataclass(frozen=True)
class OverrideReport:
    """What happened to the overrides file, for check-config and the service log."""

    path: str
    applied: bool = False
    # Why the file was not applied: None when applied or when no file exists.
    ignored: str | None = None
    mode: str | None = None
    min_duty: dict[str, float] = field(default_factory=dict)
    # Wall clock epoch seconds at which the file stops applying; None for no expiry.
    expires_at: float | None = None
    # True when the file was valid but its expires_at has passed, so the base config is used.
    expired: bool = False

    @property
    def active(self) -> bool:
        """True when the file is applied and changes something."""
        return self.applied and (self.mode is not None or bool(self.min_duty))


def _posix() -> bool:
    return os.name == "posix"


def _stat_file(path: str | Path) -> os.stat_result:
    return os.stat(path)


def _insecure_reason(st: os.stat_result) -> str | None:
    """Why a POSIX overrides file cannot be trusted, or None when it can."""
    if st.st_uid != 0:
        return "it is not owned by root"
    if st.st_mode & 0o022:
        return "it is writable by its group or by others"
    return None


# The last second of year 9999 (UTC). A later time cannot be shown by every status reader.
MAX_EXPIRES_AT = 253402300799.0


def _check_expiry(epoch: float) -> float:
    if epoch > MAX_EXPIRES_AT:
        raise ConfigError("overrides: expires_at is beyond the year 9999")
    return epoch


def _expires_at(value: object) -> float:
    """An expiry as epoch seconds: a TOML datetime with an offset, or a number."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ConfigError("overrides: expires_at needs a time zone offset, for example Z")
        try:
            return _check_expiry(value.timestamp())
        except (OverflowError, OSError, ValueError) as exc:
            raise ConfigError(f"overrides: expires_at is out of range: {exc}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("overrides: expires_at must be a datetime with an offset or epoch seconds")
    if isinstance(value, int):
        # Checked as an integer: float() of a huge int raises OverflowError.
        if value <= 0:
            raise ConfigError("overrides: expires_at must be a positive, finite time")
        if value > MAX_EXPIRES_AT:
            raise ConfigError("overrides: expires_at is beyond the year 9999")
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("overrides: expires_at must be a positive, finite time")
    return _check_expiry(float(value))


def _parse_overrides(
    data: dict, config: Config
) -> tuple[str | None, dict[str, float], float | None]:
    unknown = sorted(set(data) - {"mode", "headers", "expires_at"})
    if unknown:
        raise ConfigError(f"overrides: key {unknown[0]!r} is not allowed")
    mode = data.get("mode")
    if mode is not None and mode not in MODES:
        raise ConfigError(f"overrides: mode must be one of {', '.join(MODES)}")
    expires_at = _expires_at(data["expires_at"]) if "expires_at" in data else None
    if expires_at is not None and mode is not None:
        # A mode change needs a restart, so the service could not revert it at expiry.
        raise ConfigError("overrides: a mode cannot be time-bounded; remove expires_at or mode")
    raw = data.get("headers", {})
    if not isinstance(raw, dict):
        raise ConfigError("overrides: headers must be a table of header tables")
    by_id = {h.id: h for h in config.headers}
    floors: dict[str, float] = {}
    for hid, table in raw.items():
        where = f"overrides: header {hid}"
        header = by_id.get(hid)
        if header is None:
            raise ConfigError(f"{where} is not in the config")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        extra = sorted(set(table) - {"min_duty"})
        if extra:
            raise ConfigError(f"{where}: key {extra[0]!r} is not allowed")
        if "min_duty" not in table:
            continue
        if not header.mapped:
            raise ConfigError(f"{where} is not mapped, so its floor cannot be overridden")
        duty = _duty(table["min_duty"], f"{where}: min_duty")
        limit = header.min_duty if header.min_duty_limit is None else header.min_duty_limit
        if duty < limit:
            raise ConfigError(
                f"{where}: min_duty {duty:g} is below the allowed minimum {limit:g}"
            )
        floors[hid] = duty
    if mode == "active":
        unmapped = [h.id for h in config.headers if not h.mapped]
        if unmapped:
            raise ConfigError(
                "overrides: mode active needs every header mapped; unmapped: "
                + ", ".join(unmapped)
            )
    return mode, floors, expires_at


def apply_overrides(
    config: Config, overrides_path: str | Path | None, now: float | None = None
) -> tuple[Config, OverrideReport]:
    """Merge the optional overrides file over a validated config.

    A missing file means no overrides. A file that is not root-owned or is group or world
    writable (checked on POSIX only) is ignored. Any other problem is a ConfigError.
    The main config file is never rewritten. A file whose expires_at is not after `now`
    (wall clock epoch seconds, default the current time) is valid but no longer applies:
    the base config is returned and the report has expired set.
    """
    path = str(overrides_path or DEFAULT_OVERRIDES_PATH)
    try:
        st = _stat_file(path)
    except FileNotFoundError:
        return config, OverrideReport(path)
    except OSError as exc:
        raise ConfigError(f"cannot read overrides: {exc}") from exc
    if _posix():
        reason = _insecure_reason(st)
        if reason is not None:
            return config, OverrideReport(path, ignored=f"overrides ignored, {reason}")
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read overrides: {exc}") from exc
    except UnicodeDecodeError:
        raise
    except ValueError as exc:  # TOMLDecodeError, or an integer too long for Python to parse
        raise ConfigError(f"overrides are not valid TOML: {exc}") from exc
    mode, floors, expires_at = _parse_overrides(data, config)
    if expires_at is not None and (time.time() if now is None else now) >= expires_at:
        return config, OverrideReport(path, expires_at=expires_at, expired=True)
    headers = tuple(
        dataclasses.replace(h, min_duty=floors[h.id]) if h.id in floors else h
        for h in config.headers
    )
    merged = Config(mode=mode or config.mode, zones=config.zones, headers=headers)
    return merged, OverrideReport(
        path, applied=True, mode=mode, min_duty=floors, expires_at=expires_at
    )


def load_effective(
    path: str | Path, overrides_path: str | Path | None = None, now: float | None = None
) -> tuple[Config, OverrideReport]:
    """Load the main config and merge the overrides file over it."""
    return apply_overrides(load_config(path), overrides_path, now)
