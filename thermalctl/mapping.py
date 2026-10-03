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
