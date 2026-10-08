"""Hardware backend interface and an in-memory fake for tests.

The controller reaches hardware only through this interface. A real backend (sysfs
hwmon) implements the same four calls; tests use FakeBackend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .backends.sysfs import duty_to_pwm
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

    def released(self, header_id: str) -> bool:
        """True when the last release took effect and the header is still safe.

        False when the release failed, or when the header was released into manual mode
        and its duty register no longer holds full speed. One read at most, no write.
        """

    def holds(self, header_id: str, duty: float) -> bool:
        """True when the header's duty register still holds the value a write of duty makes.

        One read, no write. False when the value differs or cannot be read, so the caller
        writes it again.
        """

    def retake(self, header_id: str) -> None:
        """Write manual mode again, so the chip accepts duty writes after a foreign change."""


class FakeBackend:
    """In-memory backend. Set inputs, rpms and fail_reads to script a test."""

    def __init__(self) -> None:
        self.inputs: dict[str, Reading] = {}
        self.rpms: dict[str, float | None] = {}
        self.fail_reads = False
        self.writes: list[tuple[str, float]] = []
        self.releases: list[str] = []
        self.retakes: list[str] = []
        self.foreign: set[str] = set()
        # The raw 0 to 255 register value per header, as the last write left it. A test
        # changes it directly to play another program rewriting the duty.
        self.pwm: dict[str, int] = {}
        self.holds_calls: list[str] = []
        # Set by a test to play a release that did not take effect.
        self.unreleased: set[str] = set()

    def read_inputs(self) -> Mapping[str, Reading]:
        if self.fail_reads:
            raise OSError("fake read failure")
        return dict(self.inputs)

    def read_rpm(self, header_id: str) -> float | None:
        return self.rpms.get(header_id)

    def write_duty(self, header_id: str, duty: float) -> None:
        self.writes.append((header_id, duty))
        self.pwm[header_id] = duty_to_pwm(duty)

    def release(self, header_id: str) -> None:
        self.releases.append(header_id)

    def owns(self, header_id: str) -> bool:
        return header_id not in self.foreign

    def released(self, header_id: str) -> bool:
        return header_id not in self.unreleased

    def holds(self, header_id: str, duty: float) -> bool:
        self.holds_calls.append(header_id)
        return self.pwm.get(header_id) == duty_to_pwm(duty)

    def retake(self, header_id: str) -> None:
        self.retakes.append(header_id)
        self.foreign.discard(header_id)
