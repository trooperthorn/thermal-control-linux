"""Exclusive ownership lock for the fan headers.

The service holds this lock for as long as it runs, so a second writer (a hand-run
`thermalctl restore`, a `map-headers --apply`, or a second service) cannot change fan
modes underneath it. The kernel drops the lock when the holding process dies, even after
SIGKILL, so a stale lock file never blocks anything and the `ExecStopPost` helper, which
runs after the service has exited, finds it free.
"""

from __future__ import annotations

import os
from pathlib import Path

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - exercised only on Windows development hosts
    import msvcrt

LOCK_NAME = "thermalctl.lock"


class LockHeld(Exception):
    """Another process holds the ownership lock."""


def default_lock_path(state_file: str | Path) -> Path:
    """The lock lives beside the state file, in the service's runtime directory."""
    return Path(state_file).parent / LOCK_NAME


class OwnerLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self, *, create_dir: bool = False) -> None:
        """Take the lock without waiting; raise LockHeld when another holder has it."""
        if self._handle is not None:
            return
        if create_dir:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise LockHeld(f"{self.path} is held by another process") from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()

    def __enter__(self) -> "OwnerLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def is_held(path: str | Path) -> bool:
    """True when another holder has the lock. A missing lock file means nobody does."""
    if not Path(path).exists():
        return False
    probe = OwnerLock(path)
    try:
        probe.acquire()
    except LockHeld:
        return True
    probe.release()
    return False
