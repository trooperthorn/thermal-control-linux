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
