"""Guided header mapping test.

For each header the test lowers that one header, reads every fan input in the same
hwmon directory, and reports which fan fell. The operator confirms by ear or eye. The
backend state file records the originals before the first write, so even a kill in the
middle is undone by `thermalctl restore`. Hardware is touched only when the caller
runs run_mapping, which the command line does only with --apply on a terminal.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Callable
from dataclasses import dataclass

from .backends.sysfs import SysfsBackend, _read_text
from .config import Config

TEST_DUTY = 30.0


@dataclass(frozen=True)
class MappingResult:
    header_id: str
    baseline: dict[str, float]
    lowered: dict[str, float]

    @property
    def fell(self) -> list[tuple[str, float]]:
        drops = [
            (name, self.baseline[name] - self.lowered[name])
            for name in self.baseline
            if name in self.lowered
        ]
        return sorted((d for d in drops if d[1] > 0), key=lambda d: d[1], reverse=True)


def fan_inputs(pwm_path: str) -> list[str]:
    """Every fanN_input file beside a pwm file."""
    return sorted(glob.glob(os.path.join(os.path.dirname(pwm_path), "fan*_input")))


def read_fans(pwm_path: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for path in fan_inputs(pwm_path):
        try:
            out[os.path.basename(path)] = float(_read_text(path))
        except (OSError, ValueError):
            continue
    return out


def plan_lines(config: Config) -> list[str]:
    lines = [
        "Mapping plan. Nothing is written without --apply and a terminal.",
        f"For each header: hand it to manual mode, set {TEST_DUTY:g} percent, wait,"
        " compare every fan input, then restore the original mode.",
    ]
    for header in config.headers:
        fans = [os.path.basename(p) for p in fan_inputs(header.path)]
        lines.append(
            f"  {header.id}: pwm file {header.path}; fan inputs seen: "
            + (", ".join(fans) if fans else "none")
        )
    return lines


def run_mapping(
    config: Config,
    state_file: str,
    *,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    settle: Callable[[], None],
) -> list[MappingResult]:
    """Lower each header in turn and report. Restores on every exit path."""
    results: list[MappingResult] = []
    paths = {h.id: h.path for h in config.headers}
    backend = SysfsBackend(paths, {}, paths.keys(), state_file, active=True)
    with backend:
        for header in config.headers:
            answer = ask(f"Lower {header.id} to {TEST_DUTY:g} percent now? [Enter=yes, s=skip] ")
            if answer.strip().lower().startswith("s"):
                say(f"{header.id}: skipped")
                continue
            baseline = read_fans(header.path)
            try:
                backend.write_duty(header.id, TEST_DUTY)
                settle()
                lowered = read_fans(header.path)
            finally:
                backend.release(header.id)
            result = MappingResult(header.id, baseline, lowered)
            results.append(result)
            if result.fell:
                names = ", ".join(f"{n} (down {d:g} RPM)" for n, d in result.fell)
                say(f"{header.id}: fan input that fell: {names}")
            else:
                say(f"{header.id}: no fan input fell; do not mark this header as mapped")
            say(f"{header.id}: restored")
    say("All headers restored. Set mapped = true only for headers whose fan you confirmed.")
    return results


# Stall-point search -------------------------------------------------------------------

STALL_START = 100.0
STALL_STEP = 5.0
STALL_LOWEST = 10.0
STALL_RPM = 100.0
STALL_MARGIN = 10.0


@dataclass(frozen=True)
class StallResult:
    header_id: str
    stop_duty: float | None
    start_duty: float | None
    recommended: float | None
    note: str


def _rpm(path: str) -> float | None:
    try:
        return float(_read_text(path))
    except (OSError, ValueError):
        return None


def _spinning(rpm: float | None) -> bool:
    return rpm is not None and rpm >= STALL_RPM


def recommend_min_duty(stop_duty: float | None, start_duty: float | None) -> float | None:
    """The floor a header should use: above both the stop and the restart point, plus margin.

    A fan stops at one duty but often needs more to start again from rest, so the floor
    covers both. None means the fan never stopped in the range tested.
    """
    if stop_duty is None:
        return None
    lowest_spinning = stop_duty + STALL_STEP
    needed = max(lowest_spinning, start_duty if start_duty is not None else 100.0)
    return min(100.0, needed + STALL_MARGIN)


def stall_plan_lines() -> list[str]:
    return [
        f"Stall search: each mapped header steps down from {STALL_START:g} percent in "
        f"{STALL_STEP:g} percent steps to {STALL_LOWEST:g}, waiting at each step.",
        f"A fan reading below {STALL_RPM:g} RPM counts as stopped; it is set straight back to "
        "full speed, then stepped up from the stop point to find the duty that restarts it.",
        "The original mode is restored after each header, and on any exit.",
    ]


def _search_header(
    backend: SysfsBackend, header_id: str, fan: str, settle: Callable[[], None]
) -> StallResult:
    duty = STALL_START
    backend.write_duty(header_id, duty)
    settle()
    if not _spinning(_rpm(fan)):
        return StallResult(header_id, None, None, None, "fan not spinning at full speed; check it")
    stop_duty = None
    duty -= STALL_STEP
    while duty >= STALL_LOWEST:
        backend.write_duty(header_id, duty)
        settle()
        if not _spinning(_rpm(fan)):
            stop_duty = duty
            break
        duty -= STALL_STEP
    if stop_duty is None:
        return StallResult(header_id, None, None, None, f"kept spinning down to {STALL_LOWEST:g} percent")
    # The fan is now at rest, which is the state the restart search needs.
    start_duty = None
    duty = stop_duty + STALL_STEP
    while duty <= 100.0:
        backend.write_duty(header_id, duty)
        settle()
        if _spinning(_rpm(fan)):
            start_duty = duty
            break
        duty += STALL_STEP
    backend.write_duty(header_id, 100.0)
    rec = recommend_min_duty(stop_duty, start_duty)
    note = "restart point not found; using full speed" if start_duty is None else "ok"
    return StallResult(header_id, stop_duty, start_duty, rec, note)


def run_stall_search(
    config: Config,
    state_file: str,
    *,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    settle: Callable[[], None],
) -> list[StallResult]:
    """Find each mapped header's stop and restart duty. Restores on every exit path.

    Only mapped headers are tested, because the fan read is the one the mapping test
    confirmed (fanN for pwmN).
    """
    results: list[StallResult] = []
    headers = [h for h in config.headers if h.mapped]
    paths = {h.id: h.path for h in headers}
    backend = SysfsBackend(paths, {}, paths.keys(), state_file, active=True)
    with backend:
        for header in headers:
            answer = ask(f"Search the stall point of {header.id}? Its fan will stop briefly. "
                         "[Enter=yes, s=skip] ")
            if answer.strip().lower().startswith("s"):
                say(f"{header.id}: skipped")
                continue
            fan = os.path.join(os.path.dirname(header.path),
                               "fan" + os.path.basename(header.path)[len("pwm"):] + "_input")
            try:
                result = _search_header(backend, header.id, fan, settle)
            finally:
                backend.release(header.id)
            results.append(result)
            if result.recommended is None:
                say(f"{header.id}: {result.note}; current min_duty {header.min_duty:g}")
            else:
                start = "not found" if result.start_duty is None else f"{result.start_duty:g}"
                say(f"{header.id}: stops at {result.stop_duty:g} percent, restarts at {start}; "
                    f"recommended min_duty = {result.recommended:g} (now {header.min_duty:g})")
            say(f"{header.id}: restored")
    say("All headers restored. Copy the recommended min_duty values into the config by hand.")
    return results
