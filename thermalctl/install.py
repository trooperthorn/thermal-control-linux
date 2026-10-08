"""Install an overrides file safely: validate, write atomically, then signal the service.

This is the one command a delegated account (hostwatch-control) may run as root, so it
must not trust its caller. The candidate is validated by the same code the service uses,
applied to a private temporary copy that already has the final owner and mode. Only a
candidate the service would apply is moved into place, and the live file is replaced in a
single rename, so it is never half written and never changed by a rejected candidate.
"""

from __future__ import annotations

import logging
import os
import signal
import tempfile
from collections.abc import Callable
from pathlib import Path

from .config import Config, ConfigError, OverrideReport, apply_overrides, load_config

audit = logging.getLogger("thermalctl.audit")

# An overrides file is a few lines. A cap keeps a runaway pipe from filling /etc.
MAX_CANDIDATE_BYTES = 64 * 1024
FILE_MODE = 0o644


class InstallError(Exception):
    """The candidate was refused or could not be installed; the live file is unchanged."""


def chown_root(path: str | Path) -> None:
    """Make the file root-owned. Fails when not run as root, which refuses the install."""
    if not hasattr(os, "chown"):
        raise InstallError("installing an override needs a POSIX host")
    try:
        os.chown(path, 0, 0)
    except OSError as exc:
        raise InstallError(f"cannot make the file root-owned (run as root): {exc}") from exc


def signal_service(pid: int) -> None:
    """Ask the service to reload now instead of at its next file poll."""
    os.kill(pid, signal.SIGHUP)  # type: ignore[attr-defined]


def read_candidate(stream) -> bytes:
    data = stream.read(MAX_CANDIDATE_BYTES + 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_CANDIDATE_BYTES:
        raise InstallError(f"candidate is larger than {MAX_CANDIDATE_BYTES} bytes")
    if not data.strip():
        raise InstallError("candidate is empty; send an overrides file")
    return data


def install_override(
    candidate: bytes,
    config_path: str | Path,
    overrides_path: str | Path,
    *,
    now: float | None = None,
    chown: Callable[[str | Path], None] | None = None,
    validate_extra: Callable[[Config], list[str]] | None = None,
) -> OverrideReport:
    """Validate and install; return the report of the installed file or raise InstallError."""
    target = Path(overrides_path)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise InstallError(f"main config is invalid, nothing installed: {exc}") from exc
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".overrides-", suffix=".tmp", dir=target.parent)
    except OSError as exc:
        raise InstallError(f"cannot create a temporary file beside {target}: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        (chown if chown is not None else chown_root)(tmp)
        # The same function and the same ownership checks the service runs on the live file.
        try:
            merged, report = apply_overrides(config, tmp, now)
        except ConfigError as exc:
            raise InstallError(f"the service would reject this file: {exc}") from exc
        if report.ignored:
            raise InstallError(f"the service would ignore this file: {report.ignored}")
        if report.expired:
            raise InstallError(
                f"the service would ignore this file: expires_at {report.expires_at:g} has passed"
            )
        if not report.applied:
            raise InstallError("the service would not apply this file")
        if validate_extra is not None:
            problems = validate_extra(merged)
            if problems:
                raise InstallError("the service would refuse to start: " + "; ".join(problems))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(target.parent)
    report = OverrideReport(
        str(target), applied=True, mode=report.mode, min_duty=report.min_duty,
        expires_at=report.expires_at,
    )
    audit.warning(
        "override installed to %s: mode=%s min_duty=%s expires_at=%s",
        target, report.mode, report.min_duty, report.expires_at,
    )
    return report


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
