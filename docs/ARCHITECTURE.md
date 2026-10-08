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
- a temperature reading is implausible: outside the zone's plausible range (default -20
  to 150 C) or exactly 0.0, which is what a dead hwmon sensor reports, or a load reading
  is outside 0 to 100 percent (reason `invalid_input:<name>`);
- a header commanded above 0 percent, including at its floor, reports 0 RPM for longer
  than the stall window;
- a header with a non-zero `min_rpm` reports less than that for longer than the stall
  window while commanded at or above its `min_rpm_duty`;
- a header with a non-zero `min_rpm` and any commanded duty above 0 reports a speed above
  0 but below 100 RPM (a nearly stopped fan) for longer than the stall window, which
  catches a failing fan at the idle floor where the check above does not apply (reason
  `slow_fan:<id>`);
- an enabled header's `pwmN_enable` no longer holds the value this service set, which
  means something else took the header (reason `external_change:<id>`);
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

## Ownership of the headers

The service takes an exclusive lock on `thermalctl.lock` in its runtime directory
(`/run/thermalctl`, beside the state file) before it touches hardware, and holds it until
it exits. `thermalctl/lock.py` uses `flock` on POSIX. The kernel drops the lock when the
process dies, even by SIGKILL, so a stale file never blocks anything. While the lock is
held, `thermalctl restore` and `thermalctl map-headers --apply` refuse and change
nothing, and a second `thermalctl run` refuses to start. `restore --force` skips the
check; the unit uses it for `ExecStopPost`, which runs after the service has exited.

The sysfs backend remembers the `pwmN_enable` value it last wrote for each header it
controls (manual after start, the original after a release). Each cycle the controller
asks `owns(header)` for every enabled header (one read, reused by the failsafe check). If the file holds anything else, or cannot be
read, the header goes to failsafe with `external_change:<id>`, is driven to full speed,
and leaves failsafe only after the value is back and the hold period has passed.

## Header mapping test

A header is never controlled until a supervised step test has proved which fan it drives:
set one header low, observe which fan input falls, restore. Results are stored in the
config. Unmapped headers stay in firmware control.

## Status and hostwatch

The service writes `/run/thermalctl/status.json` atomically every cycle: version, state
per header, duty, RPM, zone inputs, active curve, fail-safe reasons, last change. hostwatch
reads that file read-only as a source and raises alerts for fail-safe, stall and over
temperature. hostwatch does not set targets. The document also carries `overrides_applied`
(whether an overrides file is part of the effective config), `overrides_error` (null or the
reason the last overrides reload was rejected) and the effective `min_duty` per header. It also
carries `config_error`: null, or why the last reload of the main config was rejected (an
unreadable or invalid file, with the exception type) or refused (a change that needs a
restart).

## Chip names

A header `path` or a zone `temperature_input` may be written `chip:file`, for example
`nct6779:pwm2` or `coretemp:temp1_input`. `thermalctl/hwmon.py` scans
`/sys/class/hwmon/hwmon*/name` once at start and replaces the reference with the real path,
because the hwmonN index can change across boots. A chip that is not found, is found twice,
or lacks the file makes `run` exit 1 before the lock, the state file or any fan is touched,
so every fan stays under firmware control. `map-headers` resolves names the same way. A
value with a colon and no path separator that is not a valid reference is a config error.

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
  together, and `frozen_after_s` (default 900, never below `stale_after_s`), how long a
  sensor value may stay exactly the same before the sensor counts as frozen.
- A header has `id`, `path`, `mapped` (default false), `min_duty`, `min_rpm`,
  `stall_window_s`, `zones`, a list of zone ids that must exist, and optionally
  `min_rpm_duty` (default 50), the commanded duty from which `min_rpm` is enforced.
- No two headers may name the same pwm file. The check runs when the config loads, on the
  path text after normalising separators and dot segments, and again after chip references
  are resolved, because `nct6779:pwm1` and the real path of that file are one file. Two
  headers on one file would write different duties to it and the fan would follow whichever
  wrote last. `stale_after_s` and `stall_window_s` must be finite and above 0.
- A header may also set `min_duty_limit` (default `min_duty`, never above it), the lowest
  floor an override may set.
- A zone may set `plausible_min_c` and `plausible_max_c` (defaults -20 and 150). Readings
  outside them are treated as sensor faults. `hard_max_temp_c` may not exceed the
  plausible maximum.
- Curves are lists of `[input, duty]` pairs with at least two points and strictly
  increasing input. Duty must never decrease as input rises. A temperature curve must
  reach 100 percent at or below `hard_max_temp_c`, so the hottest allowed temperature
  already means full speed. Temperature inputs must lie in -50 to 150 C, load inputs in 0 to 100
  percent, and every duty in 0 to 100.
- Ids are unique within zones and within headers.

### Overrides file

`apply_overrides` in `thermalctl/config.py` merges `/etc/thermalctl/overrides.toml` (or
`--overrides PATH`) over a validated `Config` and returns the merged config with an
`OverrideReport`; `load_effective` loads the main file and does both. The main file is
never rewritten. Only `mode` and `[headers.<id>] min_duty` are allowed. A floor must be
0 to 100, at or above the header's `min_duty_limit`, and for a mapped header that exists;
`mode = "active"` needs every header mapped. Any violation raises `ConfigError`. On POSIX
a file not owned by root or writable by group or others is ignored with an error, not
applied. A missing file is no overrides. `check-config` prints the effective values.

`Controller.reload` merges the same file over the reloaded main config. When the
controller is given a `config_path`, `cycle()` first checks for a reload: one is run when
`request_reload()` was called (the service wires it to `SIGHUP`; the handler only sets a
flag, and it is installed before the backend starts so an early signal cannot end the
process) or when the (mtime, size) fingerprint of the main config file or of the overrides
file changed, including appearing or disappearing. Any exception while merging the overrides, not only `ConfigError`, is treated this way. An
overrides error is not a main config error: the previous
effective config stays in force, no header enters failsafe, and the message is kept in
`overrides_error`. A reload whose merged mode differs from the running mode while an
overrides mode is involved is refused the same way, so a mode change from the overrides
file needs a restart, which builds the controller from the merged config. A file ignored
for ownership or permissions is reported in `overrides_error` too and the main floors
apply. The header floor is applied before the output shaper, so lowering a floor ramps
down at the normal rate and raising it takes effect in the same cycle.

#### Override expiry

`expires_at` (TOML datetime with offset, or epoch seconds) is parsed by `_expires_at`. It
is an absolute time, but the controller does not trust the wall clock alone. When it first
sees an expiry (at start or on a reload) it records a monotonic deadline, the time left by
the wall clock added to the monotonic clock, together with the wall and monotonic times of
that moment. The override ends when either clock reaches its limit, and also on any doubt:
if the wall clock and the monotonic clock have moved apart by more than
`CLOCK_STEP_TOLERANCE_S` (60 seconds) since the expiry was first seen. A wall clock stepped
backwards therefore cannot extend a lowered floor. An expiry that has ended is remembered
(`_ended_expiries`), and a reload treats a file with that `expires_at` as expired, so the
wall clock reading early cannot bring the override back. `apply_overrides`
takes `now` and returns the base config with `OverrideReport.expired` set once `now` has
reached `expires_at`. `expires_at` with `mode` is rejected, because a mode change needs a
restart and could not be reverted. At the start of every cycle `_poll_reload` also reloads
when the wall clock passes the active override's expiry, so no file change or signal is
needed. If that reload cannot retire the override (the file is unreadable, or the main
config is refused), the controller reloads once more with the overrides file left out, so
a lowered floor never outlives its end. If that reload is refused too, the controller
returns directly to its cached base config (`_revert_override_in_place`). The cache,
`_base_config`, is loaded again from the main config file when the `Controller` is
constructed, not taken from `run_service`. If the cache is empty, the base is read from disk
and held to the same `_restart_required` check as a reload; a base that needs a restart is
not applied, and the floors stay lowered with an error in the audit log. Reverting does not
always move a floor up: an override may raise a floor above the base, and that floor then
falls back to the lower base value at expiry. `expires_at` may not lie beyond the year 9999,
so every status reader can show it. The audit log gets `override active,
expires_at=...` when an override is active at start, `config change:` lines with the old
and new floor, and `override ended (...): base config in use` at expiry. A wall clock
stepped backwards no longer extends an override, and stepped forwards ends one early, which
is the safe side. The monotonic clock does not count time the host spends suspended, so an
override may outlast its wall expiry across a suspend only until the wall clock check runs
on resume, which ends it at the first cycle. The status file carries `override_active` and
`override_expires_at`.

#### Installing an override

`thermalctl/install.py` implements `thermalctl install-override`, the one command the
delegated `hostwatch-control` account may run as root (`packaging/sudoers.d/hostwatch-control`).
`install_override` loads the main config, writes the candidate to `mkstemp` beside the live
file, sets mode 0644 and root ownership on that temporary file, and then calls
`apply_overrides` on it, so ownership, parsing, floors, limits and expiry are judged by the
code and the checks the service uses on the live file. The CLI adds `validate_for_service`.
Only a file that is applied (not ignored, not expired, not rejected) is moved with
`os.replace`, and the directory is synced. Every failure removes the temporary file and
leaves the live file as it was. The service records its pid in the lock file
(`holder_pid`), and the command sends `SIGHUP` only while the lock is held. A missed signal
costs nothing, because the controller also notices the changed file on its next cycle.
While the service runs, the command also reads its status file and refuses what
`Controller.reload` would refuse: a merged mode that differs from the running mode while the
candidate or the running override sets a mode (`overrides_mode` in the status file), and any
install while the status file reports a `restart required` config error. `check-config` exits 1 for an ignored or expired overrides file, so the same condition
that makes install-override refuse also fails a check.

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
in the future, a value unchanged for longer than `frozen_after_s` (`frozen_input:<name>`), a temperature above `hard_max_temp_c`, an unreadable RPM, 0 RPM while the
commanded duty is above 0 (the floor included) for longer than `stall_window_s`, an RPM below
a non-zero `min_rpm` while commanded at or above `min_rpm_duty` for longer than
`stall_window_s` (reason `low_rpm:<id>`; `min_rpm = 0` turns this check off for fans that
may stop), a speed under 100 RPM with a non-zero `min_rpm` at any commanded duty above 0
(reason `slow_fan:<id>`), invalid config, and
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
`write_duty`, `release`, `owns`, `holds`, `retake`) and an in-memory `FakeBackend` for tests. The controller reaches
hardware only through it.

`thermalctl/controller.py` runs one cycle at a time. It reads inputs and RPMs, runs each
header's state machine, smooths each zone input with an EMA (once per zone per cycle,
before any header reads it, so headers sharing a zone see the same value and the average
moves one step per cycle), takes the largest zone duty
for the header, applies the output shaper and the minimum duty, and writes the result only
when the config mode is `active` and the header is mapped. In dry run it never calls a
backend write. Any exception in a cycle forces failsafe on every header with a
`cycle_error:<type>` reason, writes full speed (or releases to firmware) to enabled
headers, resets smoothing so the duty recovers by ramping down from 100, and the loop
continues. The next clean cycle starts the hold period, after which headers recover.

If the full speed write (or the manual mode write before it) fails for an enabled header,
the controller calls `release()` on that header at once, so the chip's own control takes
over instead of the fan keeping its last low duty. The header reports duty 0, the note
`failsafe_write_failed`, and an error line in the audit log, and the same attempt is made
again on every cycle while the header stays in failsafe, because a failed write is never
recorded as applied. Each attempt writes manual mode
(retake), fails the 255 write, then writes the original mode (release), so a stuck header
makes two `pwmN_enable` writes per cycle and is briefly in manual mode; the audit line is
written once per episode, not once per cycle. If the recorded original is manual, release
writes 255 first and falls back to mode 5 when that fails.

Write economy. The controller records, per header, the register value it last wrote and
the number of cycles since. A computed duty is written only when its 0 to 255 value
differs from the recorded one. When it is the same, one `holds(header, duty)` read of
`pwmN` confirms the register, and a mismatch (another program lowered it) is written
again in the same cycle, so an external value change is corrected within one cycle. A
write is also forced every `FORCE_REFRESH_CYCLES` (60, two minutes at the default 2
second interval) to correct any drift a read cannot show; `Controller(refresh_cycles=)`
sets it. In failsafe the controller writes manual mode and 255 (or releases to firmware)
once. On the following cycles it only verifies: the ownership read and, for full speed,
the `holds` read. For a release, the backend's `released` check replaces `holds`: it fails
when the mode write of the release failed (so a failed release is retried every cycle, as
before) and, for a header whose original mode is manual, when `pwmN` no longer holds full
speed. A changed mode or value repeats the write at once, and the write also repeats at the
forced refresh. A release into an automatic firmware mode has no duty to read back, so
only the mode is verified there. Leaving failsafe takes the header back and writes the new
duty once. Reloading a config, or dropping a header, clears the records so the next
cycle writes afresh.

The `pwmN_enable` ownership read is not reduced: it stays at one per enabled header per
cycle, because it is the only way to see an external mode change within one cycle. Failsafe
now also uses that read for its verification, so the read count is unchanged and no read
was saved. Measured on
the fake backend over 600 cycles of a steady 60 C with two headers: 600 writes per header
before (30 a minute), 10 after (0.5 a minute), with about one verification read per
header per cycle. A failsafe held for 20 cycles took 20 `pwmN_enable` writes and 20
`pwmN` writes before and takes 1 and 1 after, plus 19 verification reads.

Clocks. Every timer (the hold period, the stall, low RPM and slow fan windows, and the
staleness check) and every reading timestamp runs on a monotonic clock, `time.monotonic`
by default, so a step of the wall clock from NTP, an RTC-less boot or a VM resume can
neither stop a timer nor finish one early. The sysfs backend and the load reader stamp
readings from that clock. Only the status file uses wall time (`timestamp` and each
header's `last_change`), because hostwatch compares them with its own wall clock; the
controller converts a timer instant to wall time by its age when it writes the file.

Frozen sensor rule. Staleness and freezing are two separate checks. The sysfs backend
stamps a temperature reading with the time it was read, so `stale_after_s` (default in the
example: 10 seconds) only catches a reader that stopped reading. The reading also carries
`unchanged_since`, the time its value last differed from the one before, and a value that
has not changed for `frozen_after_s` (default 900 seconds, 15 minutes) is frozen
(`frozen_input:<name>`), which catches a chip that stopped updating but still returns its
last number. The two limits are separate because a quiet host with a 1 C sensor can hold
one reading for minutes, and tying the frozen rule to `stale_after_s` sent such a host to
full speed. The basis for the default is in UNVERIFIED.md. The sysfs interface exposes no
staleness flag, so no driver-reported staleness input exists yet. The CPU load input is stamped with the time its value was last computed. If
the `/proc/stat` counters go backwards the value is dropped and the input is missing until
a new delta exists, and if they do not move the old stamp is kept so the value ages.

The status file is written every cycle to a temp file in the same directory and renamed
over the target, so a reader sees the old or the new document and never a partial one. The
path is a constructor argument defaulting to `/run/thermalctl/status.json`; a failed write
is logged, its uniquely named temp file is removed, and the previous file is left in place.
A writer killed between creating the temp file and the rename leaves it behind, so `run`
removes every `<status name>.*.tmp` file beside the status file once it holds the ownership
lock. The file is strict JSON: a non-finite number is written as `null`. It holds version, timestamp, mode,
`config_valid`, per-zone inputs and curves, and per-header state, duty, RPM, reasons and
last change.

A reload that stops controlling a header (dry run, unmapped or removed) first drives that
header to full speed, or releases it to firmware when so configured, so it is never left
in manual PWM at a low duty. A failsafe on one header resets only the smoothing history of
its own zones.

`Controller.reload(path)` validates a new config (running it through the optional
`config_transform`, which the CLI uses to resolve chip names). Any exception while loading
counts as an invalid file, including a file that is not UTF-8 or holds a number too large
to parse; the exception type and message are published as `config_error`. It then refuses, without entering failsafe, any change the running backend
cannot follow: dry run to active, a header that becomes mapped, a mapped header whose
path changes, or a zone whose temperature input is not one the backend was started to
read. The backend reads only the inputs of the config it started with, so a zone moved to
another sensor would sit in failsafe for good; the reload is refused with a
`config_error` beginning "restart required" and the zone keeps its old sensor. The old config stays in force, the refusal is logged to `thermalctl.audit`,
`reload` returns False, and the service must be restarted to apply the change. Stopping
control (active to dry run, unmapping or removing a header) is still accepted and drives
the header to full speed first. An invalid file sets `config_valid` to
false, which puts every header in failsafe with `invalid_config`, keeps the old config
from driving anything, and is logged. Every accepted change of mode, header mapping,
paths, floors, zones or curves is logged to `thermalctl.audit` with old and new values, as
is every header state change. A reload carries the stall, low RPM and slow fan timers
(and a header's failsafe state and reasons) over to the rebuilt safety machine, so a file
that changes more often than the stall window cannot hide a stopped fan. `shutdown()` latches failsafe and writes the final status.
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
originals to the state file with an atomic rename, and only then writes manual mode. If
a state file left by a killed run is truncated, corrupt or untrusted, start does not
refuse: for every mapped header still in manual mode (or whose mode cannot be read) it
writes 255, then writes `pwmN_enable` 5 (firmware control, unverified for other chips, see `UNVERIFIED.md`),
leaves every header in another mode untouched, audits each mode change with old and new
value, renames the file to `<state file>.bad` (or `.bad.N` when one is kept) for diagnosis, and goes on to record fresh
originals. A state file that parses but whose restore fails on the hardware still blocks
start. In
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
published as `cpu_load_percent`, computed from `/proc/stat`. The first read after start
carries no delta, so it is flagged `warming_up` instead of missing; the controller then
computes from temperature alone, lists `load_warming_up` in that header's `notes` in the
status file, and does not enter failsafe. From the second cycle the load is used. A load
that cannot be read after that is still a missing input; any other `load_input` name
is rejected by `check-config` and `run`. An invalid config at start leaves every fan
untouched under firmware control and exits 1. `status` prints the status file and exits
1 when it is missing or older than `--max-age` (a finite number of 0 or more). `run --interval` must be a finite number from 0.05 to 300 seconds; anything else, including `nan` and `inf`, exits 2 before any hardware is touched. `check-config PATH` validates only.
`restore` first checks the ownership lock and exits 1 while another process holds it
(`--force` skips that check), then calls `restore_from_state_file` on the persisted originals
and exits 1 when the file is untrusted or any header fell back to full speed.

`map-headers` prints the plan by default. With `--apply` it refuses unless stdin and
stdout are terminals, then uses the backend (state file first) to lower one header at a
time to 30 percent, wait, report which fan input beside the pwm file fell, and release
the header, restoring everything on every exit path. It cannot rewrite the TOML config,
so it prints the result and the owner sets `mapped = true` by hand.

`thermalctl/notify.py` implements sd_notify over `NOTIFY_SOCKET` with the standard
library. `run` sends `READY=1` after start, `WATCHDOG=1` once per cycle and `STOPPING=1`
on the way out. `packaging/thermalctl.service` is `Type=notify` with `WatchdogSec=30`
against a 2 second cycle, `Restart=always`, a `RuntimeDirectory` holding the status and
state files, and `ExecStopPost=thermalctl restore --force --config /etc/thermalctl/config.toml`. The config and overrides are re-read on `SIGHUP`
and when either file changes on disk. The unit also sets `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`, `RestrictAddressFamilies=AF_UNIX` and a few related restrictions, and deliberately omits `ProtectKernelTunables`, `PrivateDevices` and `ProtectSystem=strict`, which could stop the pwm writes under `/sys`. These settings are untested on the hosts and listed in `UNVERIFIED.md`.
