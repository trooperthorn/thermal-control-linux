# Architecture

## Purpose and behaviour

The owner wants fans as quiet as possible when nothing demands cooling, and full power
when the host is busy or hot. Each fan header follows a curve per zone:

- A zone has a temperature input (for example the CPU package, or the hottest drive) and
  an optional load input (CPU utilization over a short window).
- The temperature curve maps temperature to a duty percentage with piecewise-linear
  points. The load curve maps load to a duty percentage the same way. The commanded duty
  is the larger of the two, so load can spin fans up early and temperature always wins.
- A header follows the largest duty among the zones assigned to it.
- Output is smoothed with an exponential moving average on the input, hysteresis on the
  output, and a ramp limit, following the semantics of Thermal Control Suite's
  ThermalController. Ramp limits apply only when slowing down; speeding up is immediate.

## Safety state machine

The controller is in one of these states per header: `dry_run`, `active`, `failsafe`.

`failsafe` means duty 100 percent, or the header's original firmware mode when the config
says so. It is entered when any of these hold:

- a zone input is missing, unreadable, not a number, or older than its staleness limit;
- a temperature exceeds the zone's hard maximum;
- a header commanded above its floor reports 0 RPM for longer than the stall window;
- the config fails validation;
- the controller is exiting for any reason.

Fail-safe is sticky until the cause has been clear for a hold period and the event is
logged. Hard floors: a minimum duty per header and a minimum RPM per header.

## Hardware backends

- **sysfs hwmon** (MediaIn-SVR, TrueNAS-SVR): reads `pwmN`, `pwmN_enable`, `fanN_input`
  and temperature inputs; writes `pwmN_enable=1` and `pwmN`. The original `pwmN_enable`
  is recorded at start and restored on exit. A systemd `ExecStopPost` helper restores it
  even after SIGKILL, and `WatchdogSec` restarts a hung loop.
- **Pi trip points** (ai-pi): generates config.txt fan trip settings for the owner to
  apply; no runtime control loop.
- **fake** (tests): an in-memory backend.

## Header mapping test

A header is never controlled until a supervised step test has proved which fan it drives:
set one header low, observe which fan input falls, restore. Results are stored in the
config. Unmapped headers stay in firmware control.

## Status and hostwatch

The service writes `/run/thermalctl/status.json` atomically every cycle: version, state
per header, duty, RPM, zone inputs, active curve, fail-safe reasons, last change. hostwatch
reads that file read-only as a source and raises alerts for fail-safe, stall and over
temperature. hostwatch does not set targets.

## Configuration and audit

A root-owned TOML file defines zones, curves, header assignments, floors and limits. It is
validated on load and on reload; an invalid file triggers fail-safe and keeps the last
valid config out of use. Every change of config, mode or header enablement is logged to
the journal with old and new values.

### Config schema

`thermalctl/config.py` loads the file with `tomllib` and raises `ConfigError` for any
problem, which the controller treats as a fail-safe cause. The top level holds `mode`
(`dry_run` or `active`, default `dry_run`), `[[zones]]` and `[[headers]]`.

- A zone has `id`, `temperature_input`, `temperature_curve`, `hard_max_temp_c`,
  `stale_after_s`, and optionally `load_input` with `load_curve`, which must be given
  together.
- A header has `id`, `path`, `mapped` (default false), `min_duty`, `min_rpm`,
  `stall_window_s` and `zones`, a list of zone ids that must exist.
- Curves are lists of `[input, duty]` pairs with at least two points and strictly
  increasing input. Temperature inputs must lie in -50 to 150 C, load inputs in 0 to 100
  percent, and every duty in 0 to 100.
- Ids are unique within zones and within headers.

`thermalctl/curves.py` interpolates linearly between points and clamps at both ends.
The zone duty is the larger of the temperature and load curve values. `docs/example.toml`
is a documented MediaIn-SVR example whose paths are unverified placeholders.

### Fail-safe and smoothing implementation

`thermalctl/safety.py` holds one `HeaderSafety` per header. Each cycle it receives the
zone readings (value and timestamp), the fan RPM, the duty last commanded and whether the
config is valid. A header is `active` only when the config mode enables it and the header
is mapped; otherwise it is `dry_run`. Any cause moves it to `failsafe` in the same cycle,
and the reasons are recorded as `kind:name` strings such as `stale_input:temp1`. Causes
are: missing, non-numeric or non-finite input, a timestamp older than `stale_after_s` or
in the future, a temperature above `hard_max_temp_c`, an unreadable RPM, 0 RPM while the
commanded duty is above `min_duty` for longer than `stall_window_s`, invalid config, and
exit. Exit latches. For every other cause the header leaves failsafe only after all
causes have been clear for the hold period, and a new cause restarts that period.
`failsafe_duty` returns 100, or 0 meaning firmware control when that mode is configured.

`thermalctl/smoothing.py` provides `Ema` for inputs, `OutputShaper` for output and
`apply_floor`. The shaper follows a rising target at once. A falling target is ignored
while it is within the hysteresis band, and otherwise the duty falls no faster than the
ramp limit, never below the target. `apply_floor` keeps duty at or above the header
minimum and passes an explicit 0 only in failsafe-to-firmware mode. The hold period,
hysteresis, ramp rate and EMA alpha are constructor arguments here; wiring them to the
config belongs to the controller slice.


### Controller loop implementation

`thermalctl/backend.py` defines the `Backend` interface (`read_inputs`, `read_rpm`,
`write_duty`, `release`) and an in-memory `FakeBackend` for tests. The controller reaches
hardware only through it.

`thermalctl/controller.py` runs one cycle at a time. It reads inputs and RPMs, runs each
header's state machine, smooths each zone input with an EMA, takes the largest zone duty
for the header, applies the output shaper and the minimum duty, and writes the result only
when the config mode is `active` and the header is mapped. In dry run it never calls a
backend write. Any exception in a cycle forces failsafe on every header with a
`cycle_error:<type>` reason, writes full speed (or releases to firmware) to enabled
headers, resets smoothing so the duty recovers by ramping down from 100, and the loop
continues. The next clean cycle starts the hold period, after which headers recover.

The status file is written every cycle to a temp file in the same directory and renamed
over the target, so a reader sees the old or the new document and never a partial one. The
path is a constructor argument defaulting to `/run/thermalctl/status.json`; a failed write
is logged, its uniquely named temp file is removed, and the previous file is left in place.
It holds version, timestamp, mode,
`config_valid`, per-zone inputs and curves, and per-header state, duty, RPM, reasons and
last change.

A reload that stops controlling a header (dry run, unmapped or removed) first drives that
header to full speed, or releases it to firmware when so configured, so it is never left
in manual PWM at a low duty. A failsafe on one header resets only the smoothing history of
its own zones.

`Controller.reload(path)` validates a new config. An invalid file sets `config_valid` to
false, which puts every header in failsafe with `invalid_config`, keeps the old config
from driving anything, and is logged. Every accepted change of mode, header mapping,
paths, floors, zones or curves is logged to `thermalctl.audit` with old and new values, as
is every header state change. `shutdown()` latches failsafe and writes the final status.
The hold period, EMA alpha, hysteresis, ramp rate and firmware-mode flag are constructor
arguments; config keys for them and the CLI wiring are still to do.
