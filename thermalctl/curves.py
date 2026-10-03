"""Piecewise-linear fan curves.

A curve is a sequence of (input, duty percent) points sorted by strictly increasing
input. Outside the first and last point the duty is clamped to the end values, so a
reading colder than the first point never asks for less than that point's duty and a
reading hotter than the last point never asks for more than its duty.
"""

from __future__ import annotations

from collections.abc import Sequence

Point = tuple[float, float]


def interpolate(points: Sequence[Point], value: float) -> float:
    """Return the duty for value, clamped at both ends of the curve."""
    if not points:
        raise ValueError("a curve needs at least one point")
    if value <= points[0][0]:
        return float(points[0][1])
    if value >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return float(points[-1][1])  # unreachable for sorted points


def zone_duty(
    temperature_curve: Sequence[Point],
    temperature: float,
    load_curve: Sequence[Point] | None = None,
    load: float | None = None,
) -> float:
    """Return max(temperature curve, load curve).

    Load can only raise the duty. When the zone has no load curve, or the load reading
    is absent, the temperature curve alone decides. Callers treat a missing load input
    as a fail-safe matter; this function never lowers the temperature duty.
    """
    duty = interpolate(temperature_curve, temperature)
    if load_curve is not None and load is not None:
        duty = max(duty, interpolate(load_curve, load))
    return duty
