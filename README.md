# thermal-control-linux

A small root service that sets fan speeds from a load-aware curve. Fans stay slow while
the host is cool and idle, and rise to full speed as temperature or CPU load climbs. On
any error, stale sensor, stalled fan or exit, fans go to full speed or back to firmware
control. The design is in `docs/ARCHITECTURE.md`; hardware facts that have not been
measured are listed in `UNVERIFIED.md`.

## Commands

- `thermalctl run --config PATH` runs the control loop. It is a dry run unless the config
  says `mode = "active"`, and even then it writes only to headers marked `mapped = true`.
- `thermalctl run` and `check-config` also read an optional overrides file, `/etc/thermalctl/overrides.toml` by default or `--overrides PATH`. See the overrides section below.
- `thermalctl status` prints the status file the service writes: each zone's temperature and CPU load, then each header's state, duty, RPM and reasons. `--json` prints it raw.
  It exits non-zero when the file is missing or older than `--max-age` seconds.
- `thermalctl check-config PATH` validates a config file and changes nothing. It rejects curves whose duty falls as the input rises and temperature curves that do not reach 100 percent at or below `hard_max_temp_c`.
- `thermalctl restore` refuses while the running service holds the ownership lock; `--force`
  skips that check and is what the unit uses after the service has exited. It reads the persisted original fan modes and puts them back. The
  systemd unit runs it as `ExecStopPost`, so it also runs after the service was killed.
  It writes full speed (pwm 255) to each header before restoring its mode, so a header
  whose original mode was manual (1) is left at full speed, not at its last low duty.
- `thermalctl map-headers --config PATH` prints the plan for the header mapping test. With
  `--apply --find-stall` it measures each mapped fan's stop and restart duty.
  With `--apply`, and only on a terminal, it lowers one header at a time, shows which fan
  input fell, and restores the original mode before moving on. It refuses while the
  service holds the ownership lock, so stop the service first.

## Install on Debian

These steps assume a root shell and a host whose fan chip is already exposed through
hwmon. Confirm the paths with the commands in `UNVERIFIED.md` first.

1. Install Python and the venv module: `apt install python3 python3-venv`.
2. Create the environment and install this repository into it:
   `python3 -m venv /opt/thermalctl/venv` and then
   `/opt/thermalctl/venv/bin/pip install /path/to/thermal-control-linux`.
3. Copy `docs/example.toml` to `/etc/thermalctl/config.toml`, make it owned by root and
   writable by root only, and replace every placeholder path with the measured one.
4. Check it: `/opt/thermalctl/venv/bin/thermalctl check-config /etc/thermalctl/config.toml`.
5. Install the unit: copy `packaging/thermalctl.service` to
   `/etc/systemd/system/thermalctl.service`, then run `systemctl daemon-reload`.
6. Start it in dry run: `systemctl enable --now thermalctl`.

## Dry run first

The config ships with `mode = "dry_run"` and every header with `mapped = false`. In that
state the service reads sensors, computes duties, logs and writes the status file, but
never touches a fan. Leave it there and compare the computed duties in the status file
with the temperatures for a while. Only then run the mapping test for each header:

    thermalctl map-headers --config /etc/thermalctl/config.toml
    thermalctl map-headers --config /etc/thermalctl/config.toml --apply

The first command only prints the plan. Set `mapped = true` on a header only after you
have confirmed which fan it drives, and change `mode` to `"active"` last.

Many fans stop below some duty, and need more than that to start again from rest. Before
going active, find each mapped header's floor with the service stopped:

    systemctl stop thermalctl
    thermalctl map-headers --config /etc/thermalctl/config.toml --apply --find-stall

Each mapped header steps down from 100 percent in 5 percent steps until its fan stops,
then steps up until it restarts, and the result is a recommended `min_duty`: the higher
of the two points plus a 10 percent margin. The fan sits stopped for up to about half a
minute per header, so run it at idle. Nothing is written to the config; copy the values
in by hand. A change of
mode, mapping, curve or floor is written to the journal with the old and new values.
The service reads its config at start. A reload that switches from dry run to active, or
maps a header or changes a mapped header's path, is refused and logged; restart the service
to apply those changes.

## Overrides file

A root-owned file, `/etc/thermalctl/overrides.toml`, may change two things without
rewriting the main config: the `mode` and, per header, `min_duty`. It is merged over the
main config at start and on reload. Any other key is rejected.

    mode = "active"

    [headers.pwm2]
    min_duty = 20

Each header may set `min_duty_limit` in the main config, the lowest floor an override may
set; it defaults to the header's own `min_duty` and may not be above it. An override below
the limit, above 100, for an unknown header or for an unmapped header is rejected, and so
is `mode = "active"` while any header is unmapped. A rejected file is a config error, so
`run` exits 1 and leaves the fans under firmware control. On POSIX the file must be owned
by root and not writable by its group or others; otherwise it is ignored and an error is
printed. A missing file means no overrides. `check-config` prints whether overrides were
applied and the effective `min_duty` of every header.

The running service re-reads the overrides file, and the main config, on `SIGHUP` and
whenever the overrides file's modification time or size changes or the file appears or
disappears, at the start of the next cycle. A new `min_duty` applies at once: a higher
floor takes effect immediately and a lower one ramps down at the normal rate. A mode change
in the overrides file is refused while running and logged; restart the service to apply
it. A rejected overrides file (bad value, unknown header, wrong owner) leaves the previous
effective config in force, does not trigger fail-safe, and its reason is written to the
status file as `overrides_error`, which clears when a good file is read.

## Header names and ownership

Write header paths as `chip:pwmN` (for example `nct6779:pwm2`) and zone inputs as
`chip:tempN_input`. The service resolves the chip by its hwmon `name` at start, because the
hwmonN number can change across boots. If the chip is missing it exits without touching any
fan. The service holds a lock under `/run/thermalctl` while it runs, and it puts a header
in fail-safe if something else changes that header's `pwmN_enable`.

## Stall and minimum RPM

A fan commanded above 0 percent, including at its floor duty, that reads 0 RPM for longer
than `stall_window_s` puts its header in fail-safe. A fan reading below `min_rpm` while
commanded at or above `min_rpm_duty` (default 50 percent) for that window does too. Set
`min_rpm = 0` for a fan that may legitimately stop, which turns the RPM floor off. A fan
that turns slower than 100 RPM at any commanded duty above zero, including the idle floor,
trips fail-safe after the same window (`slow_fan`), unless `min_rpm` is 0.

If the full speed write fails during fail-safe, the header is handed back to firmware
control and the status file reports the note `failsafe_write_failed`. If the state file
from a killed run is corrupt, the service puts every mapped header under firmware control,
keeps the bad file as `state.json.bad`, and starts.

## hostwatch integration

The service writes `/run/thermalctl/status.json` atomically every cycle. hostwatch reads
that file read-only and raises alerts for fail-safe, stall and over temperature; it never
sets a target or writes to hardware. Point hostwatch at that path as a file source. The
document holds the version, timestamp, mode, per-zone temperature and load, and per
header state, duty, RPM, effective `min_duty` and fail-safe reasons, plus `overrides_applied` and `overrides_error` at the top level. `thermalctl status` shows the same data
for a person at a shell.
