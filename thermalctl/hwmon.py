"""Resolve `chip:file` names to the current hwmonN directory.

The kernel numbers hwmon devices in probe order, so `hwmon1` may be a different chip
after a reboot. A header path written as `nct6779:pwm2` (or a temperature input written
as `coretemp:temp1_input`) names the chip by the contents of its `name` file and is
resolved once at start. An unknown or ambiguous chip raises HwmonError and the caller
must not touch any hardware.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .config import CHIP_REF, Config

DEFAULT_HWMON_ROOT = "/sys/class/hwmon"


class HwmonError(Exception):
    """A chip name could not be resolved to exactly one hwmon directory."""


def is_chip_ref(value: str) -> bool:
    return CHIP_REF.match(value) is not None


def chip_directories(root: str | Path) -> dict[str, list[Path]]:
    """Map each chip name to the hwmon directories that report it."""
    found: dict[str, list[Path]] = {}
    base = Path(root)
    if not base.is_dir():
        return found
    for entry in sorted(base.iterdir()):
        try:
            name = (entry / "name").read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            continue
        found.setdefault(name, []).append(entry)
    return found


def resolve_ref(value: str, root: str | Path = DEFAULT_HWMON_ROOT) -> str:
    """Return the real path for `chip:file`, or the value itself when it is a plain path."""
    match = CHIP_REF.match(value)
    if match is None:
        return value
    chip, filename = match.group("chip"), match.group("file")
    directories = chip_directories(root).get(chip, [])
    if not directories:
        raise HwmonError(f"hwmon chip {chip!r} not found under {root} (needed for {value})")
    if len(directories) > 1:
        raise HwmonError(f"hwmon chip {chip!r} matches {len(directories)} devices; refusing to guess")
    target = directories[0] / filename
    if not target.exists():
        raise HwmonError(f"{target} does not exist (needed for {value})")
    return target.as_posix()


def resolve_config(config: Config, root: str | Path = DEFAULT_HWMON_ROOT) -> Config:
    """Return the config with every chip reference replaced by its current path.

    Input names in zones stay as written when they are plain paths. A chip reference in
    a zone input is replaced by the resolved path, which is also what the backend reads.
    """
    zones = tuple(
        dataclasses.replace(z, temperature_input=resolve_ref(z.temperature_input, root))
        for z in config.zones
    )
    headers = tuple(
        dataclasses.replace(h, path=resolve_ref(h.path, root)) for h in config.headers
    )
    return dataclasses.replace(config, zones=zones, headers=headers)
