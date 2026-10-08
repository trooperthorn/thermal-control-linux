"""CPU load input read from /proc/stat, added on top of any backend.

The load figure is the share of non-idle time between two reads, as a percent. It is
published under the input name `cpu_load_percent`, which zones name in `load_input`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from .backend import Backend
from .safety import Reading

LOAD_INPUT = "cpu_load_percent"
SUPPORTED_LOAD_INPUTS = (LOAD_INPUT,)


def read_cpu_times(proc_stat: str) -> tuple[float, float]:
    """Return (busy, total) jiffies from the aggregate cpu line."""
    with open(proc_stat, encoding="ascii") as handle:
        fields = handle.readline().split()
    if not fields or fields[0] != "cpu":
        raise ValueError("first line is not the aggregate cpu line")
    values = [float(x) for x in fields[1:]]
    total = sum(values[:8])
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    return total - idle, total


class LoadBackend:
    """Wraps a backend and adds the CPU load input. Everything else passes through."""

    def __init__(
        self,
        inner: Backend,
        proc_stat: str = "/proc/stat",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.proc_stat = proc_stat
        self.clock = clock
        self._last: tuple[float, float] | None = None
        self._value: float | None = None
        # When _value was computed. A reading is stamped with this, never with the read time,
        # so a value that is no longer being refreshed ages and goes stale.
        self._value_time: float | None = None
        self._errored = False
        self._reads = 0
        self._sample()  # Prime, so the first cycle already has a delta to work with.

    def _sample(self) -> None:
        try:
            busy, total = read_cpu_times(self.proc_stat)
        except (OSError, ValueError, IndexError):
            self._errored = True
            self._last = None
            self._value = None
            self._value_time = None
            return
        if self._last is not None:
            d_total = total - self._last[1]
            if d_total > 0:
                self._value = max(0.0, min(100.0, 100.0 * (busy - self._last[0]) / d_total))
                self._value_time = self.clock()
            elif d_total < 0:
                # The counters went backwards (CPU hotplug, a reset). The old value no
                # longer describes anything, so report none until a new delta exists.
                self._value = None
                self._value_time = None
            # d_total == 0: no time passed between reads; keep the value and its old stamp.
        self._last = (busy, total)

    def read_inputs(self) -> Mapping[str, Reading]:
        result = dict(self.inner.read_inputs())
        self._sample()
        self._reads += 1
        if self._reads == 1 and not self._errored:
            # The first cycle reuses no delta, so the controller works from temperature
            # alone. This is warm-up, not a fault.
            result[LOAD_INPUT] = Reading(None, None, warming_up=True)
        elif self._value is None or self._value_time is None:
            result[LOAD_INPUT] = Reading(None, None)
        else:
            result[LOAD_INPUT] = Reading(self._value, self._value_time)
        return result

    def read_rpm(self, header_id: str) -> float | None:
        return self.inner.read_rpm(header_id)

    def write_duty(self, header_id: str, duty: float) -> None:
        self.inner.write_duty(header_id, duty)

    def release(self, header_id: str) -> None:
        self.inner.release(header_id)

    def owns(self, header_id: str) -> bool:
        return self.inner.owns(header_id)

    def released(self, header_id: str) -> bool:
        return self.inner.released(header_id)

    def holds(self, header_id: str, duty: float) -> bool:
        return self.inner.holds(header_id, duty)

    def retake(self, header_id: str) -> None:
        self.inner.retake(header_id)
