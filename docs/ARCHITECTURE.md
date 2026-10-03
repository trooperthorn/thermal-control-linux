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
- a header commanded above 0 percent, including at its floor, reports 0 RPM for longer
  than the stall window;
- a header with a non-zero `min_rpm` reports less than that for longer than the stall
  window while commanded at or above its `min_rpm_duty`;
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
  `stall_window_s`, `zones`, a list of zone ids that must exist, and optionally
  `min_rpm_duty` (default 50), the commanded duty from which `min_rpm` is enforced.
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
commanded duty is above 0 (the floor included) for longer than `stall_window_s`, an RPM below
a non-zero `min_rpm` while commanded at or above `min_rpm_duty` for longer than
`stall_window_s` (reason `low_rpm:<id>`; `min_rpm = 0` turns this check off for fans that
may stop), invalid config, and
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
arguments; config keys for them are still to do; the CLI uses the defaults.

### sysfs backend implementation

`thermalctl/backends/sysfs.py` implements the `Backend` interface over hwmon files. It is
given the pwm path of each header, a map from input names to files, the ids of mapped
headers, a state file path and whether the config mode is active. Temperature files are
read as millidegrees and divided by a scale. An unreadable or non-numeric input becomes a
reading with no value, which the safety machine treats as a missing input. An unreadable
fan file gives an RPM of None.

On `start()` in active mode it reads `pwmN_enable` for every mapped header, saves the
originals to the state file with an atomic rename, and only then writes manual mode. In
dry run, or for an unmapped header, it never writes anything. `write_duty` scales the duty
percent to 0 to 255, clamps it, and treats a non-number as full speed. `restore()` writes
255 to each pwm file first and then the recorded original `pwmN_enable`. A header whose
original mode was manual (1) is therefore left at full speed, not at the last low duty;
it stays at full speed until something sets it again. If the mode write fails the 255
already written stands, and the state file is kept when any header fell back. The backend is a context manager so
normal exit and exceptions restore, and `install_signal_handlers()` turns SIGTERM and
SIGINT into `SystemExit` so they restore too. After a SIGKILL, `restore_from_state_file()`
applies the saved originals, and is meant to be called by the `ExecStopPost` helper. The
CLI and the systemd unit use it as described below.

### Command line, unit and restore helper

`thermalctl/cli.py` has five commands, each returning an exit code. `run --config PATH`
loads the config, builds the sysfs backend (temperature inputs are the file paths named
by the zones, and only mapped headers are controlled in active mode), wraps it with the
CPU load reader from `thermalctl/load.py`, and runs the controller. The load input is
published as `cpu_load_percent`, computed from `/proc/stat`; any other `load_input` name
is rejected by `check-config` and `run`. An invalid config at start leaves every fan
untouched under firmware control and exits 1. `status` prints the status file and exits
1 when it is missing or older than `--max-age`. `check-config PATH` validates only.
`restore` calls `restore_from_state_file` on the persisted originals and exits 1 when the
file is untrusted or any header fell back to full speed.

`map-headers` prints the plan by default. With `--apply` it refuses unless stdin and
stdout are terminals, then uses the backend (state file first) to lower one header at a
time to 30 percent, wait, report which fan input beside the pwm file fell, and release
the header, restoring everything on every exit path. It cannot rewrite the TOML config,
so it prints the result and the owner sets `mapped = true` by hand.

`thermalctl/notify.py` implements sd_notify over `NOTIFY_SOCKET` with the standard
library. `run` sends `READY=1` after start, `WATCHDOG=1` once per cycle and `STOPPING=1`
on the way out. `packaging/thermalctl.service` is `Type=notify` with `WatchdogSec=30`
against a 2 second cycle, `Restart=always`, a `RuntimeDirectory` holding the status and
state files, and `ExecStopPost=thermalctl restore`. The config is read at start only;
there is no reload signal yet.
