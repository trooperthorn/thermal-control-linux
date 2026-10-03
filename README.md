# thermal-control-linux

A small root service that sets fan speeds from a load-aware curve. Fans stay slow while
the host is cool and idle, and rise to full speed as temperature or CPU load climbs. On
any error, stale sensor, stalled fan or exit, fans go to full speed or back to firmware
control. The design is in `docs/ARCHITECTURE.md`; hardware facts that have not been
measured are listed in `UNVERIFIED.md`.

## Commands

- `thermalctl run --config PATH` runs the control loop. It is a dry run unless the config
  says `mode = "active"`, and even then it writes only to headers marked `mapped = true`.
- `thermalctl status` prints the status file the service writes. `--json` prints it raw.
  It exits non-zero when the file is missing or older than `--max-age` seconds.
- `thermalctl check-config PATH` validates a config file and changes nothing. It rejects curves whose duty falls as the input rises and temperature curves that do not reach 100 percent at or below `hard_max_temp_c`.
- `thermalctl restore` reads the persisted original fan modes and puts them back. The
  systemd unit runs it as `ExecStopPost`, so it also runs after the service was killed.
  It writes full speed (pwm 255) to each header before restoring its mode, so a header
  whose original mode was manual (1) is left at full speed, not at its last low duty.
- `thermalctl map-headers --config PATH` prints the plan for the header mapping test.
  With `--apply`, and only on a terminal, it lowers one header at a time, shows which fan
  input fell, and restores the original mode before moving on.

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
have confirmed which fan it drives, and change `mode` to `"active"` last. A change of
mode, mapping, curve or floor is written to the journal with the old and new values.
The service reads its config at start; restart it to apply a change.

## Stall and minimum RPM

A fan commanded above 0 percent, including at its floor duty, that reads 0 RPM for longer
than `stall_window_s` puts its header in fail-safe. A fan reading below `min_rpm` while
commanded at or above `min_rpm_duty` (default 50 percent) for that window does too. Set
`min_rpm = 0` for a fan that may legitimately stop, which turns the RPM floor off.

## hostwatch integration

The service writes `/run/thermalctl/status.json` atomically every cycle. hostwatch reads
that file read-only and raises alerts for fail-safe, stall and over temperature; it never
sets a target or writes to hardware. Point hostwatch at that path as a file source. The
document holds the version, timestamp, mode, per-zone temperature and load, and per
header state, duty, RPM and fail-safe reasons. `thermalctl status` shows the same data
for a person at a shell.
