"""Input and output smoothing for fan duty.

The semantics follow Thermal Control Suite's ThermalController: an exponential moving
average on the input, hysteresis on the output, and a ramp limit. Speeding up is never
delayed: hysteresis and the ramp limit only ever apply to a decrease in duty.
"""

from __future__ import annotations

import math


class Ema:
    """Exponential moving average. An alpha of 1 means no smoothing."""

    def __init__(self, alpha: float) -> None:
        if not (math.isfinite(alpha) and 0.0 < alpha <= 1.0):
            raise ValueError("alpha must be greater than 0 and at most 1")
        self.alpha = alpha
        self.value: float | None = None

    def update(self, sample: float) -> float:
        if not math.isfinite(sample):
            raise ValueError("sample must be finite")
        if self.value is None:
            self.value = float(sample)
        else:
            self.value += self.alpha * (sample - self.value)
        return self.value

    def reset(self) -> None:
        """Forget history, for example after a fail-safe, so old readings cannot linger."""
        self.value = None


class OutputShaper:
    """Hysteresis and ramp limit on the commanded duty.

    A rising target is followed at once. A falling target is ignored while it is less
    than hysteresis percent below the current duty, and beyond that the duty falls by
    at most ramp_down_per_s percent per second. The output therefore never drops below
    the target, so it never sits lower than the curve asks for at the current reading.
    """

    def __init__(self, hysteresis: float, ramp_down_per_s: float) -> None:
        if not (math.isfinite(hysteresis) and hysteresis >= 0.0):
            raise ValueError("hysteresis must be 0 or more")
        if not (math.isfinite(ramp_down_per_s) and ramp_down_per_s > 0.0):
            raise ValueError("ramp_down_per_s must be greater than 0")
        self.hysteresis = hysteresis
        self.ramp_down_per_s = ramp_down_per_s
        self.duty: float | None = None

    def apply(self, target: float, dt: float) -> float:
        if not (math.isfinite(target) and math.isfinite(dt)) or dt < 0:
            raise ValueError("target and dt must be finite and dt not negative")
        target = min(100.0, max(0.0, target))
        if self.duty is None or target >= self.duty:
            self.duty = target
        elif self.duty - target >= self.hysteresis:
            self.duty = max(target, self.duty - self.ramp_down_per_s * dt)
        return self.duty

    def reset(self, duty: float | None = None) -> None:
        self.duty = duty


def apply_floor(duty: float, min_duty: float, allow_zero: bool = False) -> float:
    """Clamp duty to the header minimum and to 100.

    An explicit 0 is passed through only when allow_zero is set, which is the
    fail-safe-to-firmware mode where 0 means hand the header back to firmware.
    """
    if not math.isfinite(duty):
        return 100.0
    if allow_zero and duty == 0:
        return 0.0
    return min(100.0, max(min_duty, duty))
