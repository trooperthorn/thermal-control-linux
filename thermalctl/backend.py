"""Hardware backend interface and an in-memory fake for tests.

The controller reaches hardware only through this interface. A real backend (sysfs
hwmon) implements the same four calls; tests use FakeBackend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .safety import Reading


class Backend(Protocol):
    def read_inputs(self) -> Mapping[str, Reading]:
        """Return every named sensor reading with its timestamp."""

    def read_rpm(self, header_id: str) -> float | None:
        """Return the fan RPM for a header, or None when unreadable."""

    def write_duty(self, header_id: str, duty: float) -> None:
        """Set manual mode and a duty percent on a header."""

    def release(self, header_id: str) -> None:
        """Hand a header back to firmware control."""

    def owns(self, header_id: str) -> bool:
        """False when something else changed the header's mode since this service set it."""


class FakeBackend:
    """In-memory backend. Set inputs, rpms and fail_reads to script a test."""

    def __init__(self) -> None:
        self.inputs: dict[str, Reading] = {}
        self.rpms: dict[str, float | None] = {}
        self.fail_reads = False
        self.writes: list[tuple[str, float]] = []
        self.releases: list[str] = []
        self.foreign: set[str] = set()

    def read_inputs(self) -> Mapping[str, Reading]:
        if self.fail_reads:
            raise OSError("fake read failure")
        return dict(self.inputs)

    def read_rpm(self, header_id: str) -> float | None:
        return self.rpms.get(header_id)

    def write_duty(self, header_id: str, duty: float) -> None:
        self.writes.append((header_id, duty))

    def release(self, header_id: str) -> None:
        self.releases.append(header_id)

    def owns(self, header_id: str) -> bool:
        return header_id not in self.foreign
