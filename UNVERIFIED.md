# Unverified assumptions

Each row is believed but not measured. Remove a row only after measuring it, and say
how in the commit message.

## Measured

Measured on MediaIn-SVR on 2026-10-03 by the owner, read-only. The hwmonN index can
change across boots, so match by the `name` file.

| Fact | Value |
|---|---|
| hwmon names | hwmon0 acpitz, hwmon1 nct6779, hwmon2 asus, hwmon3 coretemp |
| pwm1 | 153, pwm1_enable 5, pwm1_mode 1, fan1 974 RPM |
| pwm2 | 63, pwm2_enable 5, pwm2_mode 1, fan2 1428 RPM |
| pwm3 | 153, pwm3_enable 5, pwm3_mode 1, fan3 1033 RPM |
| pwm4 | 153, pwm4_enable 5, pwm4_mode 1, fan4 1141 RPM |
| pwm5 | 255, pwm5_enable 0, pwm5_mode 1, fan5 0 RPM (no fan fitted) |
| Fan type | All fans are 4-pin |

The meaning of the pwmN_enable values (5 and 0 here) is still unverified; see below.

## Unverified

| Assumption | Verify with |
|---|---|
| MediaIn-SVR exposes pwm1 to pwm5 with pwmN_enable through nct6775 | `ls /sys/class/hwmon/*/pwm*; for f in /sys/class/hwmon/*/pwm*_enable; do echo $f $(cat $f); done` |
| pwmN_enable 1 is manual on nct6775 and 0 is full speed | Driver documentation plus a supervised test on one header |
| TrueNAS-SVR has a Nuvoton NCT679x chip that nct6775 binds | `cat /sys/class/dmi/id/board_name; sudo modprobe nct6775; ls /sys/class/hwmon/*/name; sudo dmesg \| grep -i -E 'nct\|resource conflict'` |
| The Pi 5 fan is controlled by kernel trip points set in config.txt | `ls /sys/class/thermal/cooling_device*; cat /sys/class/thermal/cooling_device*/type; grep -i fan /boot/firmware/config.txt` |
| `coretemp:temp1_input` in `docs/example.toml` is the CPU package sensor on MediaIn-SVR (the chip names and the pwm1 to pwm5 files are measured above, which input is the package is not) | `cat /sys/class/hwmon/hwmon*/name; cat /sys/class/hwmon/hwmon3/temp*_label` (match the hwmon index to coretemp first) |
| Which fan each of pwm1 to pwm4 drives on MediaIn-SVR | `thermalctl map-headers --config PATH --apply` on a terminal, one supervised header at a time |
| Setting `min_rpm = 0` is the right way to handle a fan that stops by design, and the default `min_rpm_duty` of 50 percent is high enough that a healthy fan always exceeds `min_rpm` there | Supervised step test per header: note `fanN_input` at 50 percent and at the floor |
| The example curve points and floors (min duty 20, min RPM 300, stall window 15 s) suit the installed 4-pin fans | Supervised step test per header while watching `fanN_input` and temperatures |
| The default fail-safe hold period, hysteresis, ramp-down rate and EMA alpha suit the installed fans and sensors | Supervised step test per header while watching temperatures and `fanN_input` for hunting or slow recovery |
| Writing 100 percent duty (or releasing to firmware) on every enabled header is the correct response to a failed cycle on the real fans and driver | Supervised test: make a read fail on MediaIn-SVR and watch `fanN_input` rise |
| The controller's default cycle interval and status path `/run/thermalctl/status.json` suit hostwatch and the systemd `RuntimeDirectory` | Check the unit once written and hostwatch's configured source path |
| The meanings of nct6775 `pwmN_enable` values used by the sysfs backend (1 manual, other values such as 5 for the firmware smart mode, and which value means full speed) and that restoring the recorded value returns control to firmware | Kernel hwmon sysfs-interface documentation for the driver, then on one supervised header: record `cat pwmN_enable`, set 1, write pwmN, restore, and watch `fanN_input` |
| After `thermalctl restore`, a header whose original `pwmN_enable` is 1 stays at 255 (full speed) until something changes it | Supervised test: record `pwmN_enable`, run the service, `kill -9`, run `thermalctl restore`, then read `pwmN` and `fanN_input` |
| Writing 255 to pwmN gives full fan speed on the 4-pin fans after a failed restore, and the fan reads the pwm file as 0 to 255 | Supervised test: set manual, write 255 and 0 to one header, watch `fanN_input` |
| The unit's install paths (`/opt/thermalctl/venv/bin/thermalctl`, `/etc/thermalctl/config.toml`), `Type=notify` with `NotifyAccess=main`, `WatchdogSec=30` and `RuntimeDirectoryPreserve=yes` behave as intended on Debian and TrueNAS | `systemd-analyze verify /etc/systemd/system/thermalctl.service`, then `systemctl start thermalctl; systemctl show thermalctl -p WatchdogUSec,NotifyAccess`, then `kill -9` the main process and confirm `pwmN_enable` returns to its recorded value |
| `/proc/stat` utilization over one cycle is a suitable load input, and the 2 second cycle default suits the fans | Compare `thermalctl status --json` load against `top` under a known load |
| Lowering to 30 percent and an 8 second settle shows a clear RPM drop on the installed 4-pin fans during `map-headers --apply`, and fan inputs sit in the same hwmon directory as the pwm files | Run `map-headers --apply` on one supervised header and compare against `ls /sys/class/hwmon/*/fan*_input` |
| The default plausible temperature range of -20 to 150 C, and that an exact 0.0 from a temperature input always means a dead sensor, hold for the sensors on MediaIn-SVR and TrueNAS-SVR | Read every configured `temp*_input` at idle and under load, and note any that legitimately read 0 or exceed the range |
| The ownership lock (`flock` on `/run/thermalctl/thermalctl.lock`) is refused to a second process on Debian and TrueNAS, is released when the service is killed with SIGKILL, and `ExecStopPost=thermalctl restore --force` runs after that | `thermalctl run` in one shell, `thermalctl restore` in another (expect a refusal), then `kill -9` the service and confirm `systemctl status thermalctl` shows ExecStopPost succeeded |
| Another program that changes a `pwmN_enable` is detected within one cycle and the header goes to full speed | Supervised test: run active on one mapped header, `echo 2 > pwmN_enable` from a shell, and watch `thermalctl status` and `fanN_input` |
| A fan commanded above 0 that reads under 100 RPM (`NEAR_STOP_RPM` in `thermalctl/safety.py`) is failing, never healthy, at the idle floor on every installed fan. The lowest measured speed is 974 RPM at 60 percent duty (fan1), nothing is measured at the floor, so 100 RPM is a margin far below any measured value, not a measured limit | Supervised step test per header: note `fanN_input` at the floor (min_duty) and at 0 percent if the fan stops, and keep the threshold below the slowest healthy reading |
| Writing `pwmN_enable` 5 returns a header to chip control on nct6779 when the state file is unusable (the real original is unknown, so 5 is assumed from the measured value on all four headers), and writing 255 to pwmN first gives full speed while the header is still manual | Supervised test: truncate the state file after a `kill -9`, start the service, and read `pwmN_enable` and `fanN_input` |
| Releasing a header to its recorded `pwmN_enable` after a failed full speed write leaves the fan at the chip's own speed, never slower than the controller had it | Supervised test: make the pwmN write fail, trip fail-safe, and watch `fanN_input` |
| A temperature sensor on these hosts changes its value at least once within `frozen_after_s` (900 seconds in the example), so the frozen sensor rule does not send an idle host to full speed. The 15 minute default is a chosen margin for a 1 C sensor on an idle host, not a measured figure | Read `temp1_input` of the CPU chip once a second for an hour at idle and note the longest run of one value; set `frozen_after_s` well above it |
| The hwmon driver exposes no flag that says a temperature value is stale, so the frozen rule relies on the value-unchanged check alone | Look for a staleness or alarm attribute next to `temp1_input` in the CPU chip directory (`ls /sys/class/hwmon/hwmonN/`) |
| The unit's hardening (`NoNewPrivileges=yes`, `ProtectSystem=full`, `ProtectHome=yes`, `PrivateTmp=yes`, `RestrictAddressFamilies=AF_UNIX`, `RestrictSUIDSGID=yes`, `RestrictRealtime=yes`, `LockPersonality=yes`) leaves the sysfs `pwmN` and `pwmN_enable` writes, the status and state files under `/run/thermalctl`, the sd_notify socket and `ExecStopPost` working. `ProtectKernelTunables`, `PrivateDevices` and `ProtectSystem=strict` are left out on the assumption that they could make `/sys` read-only | `systemd-analyze verify /etc/systemd/system/thermalctl.service`, `systemd-analyze security thermalctl`, then start the service in active mode on one mapped header and confirm `pwmN` changes, `thermalctl status` is fresh and `systemctl stop thermalctl` restores `pwmN_enable` |
| The sudoers example `packaging/sudoers.d/hostwatch-control` passes `visudo -cf`, and sudo permits only `thermalctl install-override` with no further arguments for `hostwatch-control` (arguments written in a rule are matched exactly) | `visudo -cf packaging/sudoers.d/hostwatch-control`, then as that account `sudo -n /opt/thermalctl/venv/bin/thermalctl install-override --overrides /tmp/x < candidate.toml` must be refused, and the plain form must work |
| `thermalctl install-override` run through sudo ends with `/etc/thermalctl/overrides.toml` owned by root with mode 0644, and the running service reloads on the `SIGHUP` it sends | Install a candidate with a 2 minute `expires_at`, run `ls -l /etc/thermalctl/overrides.toml`, check `thermalctl status` for the active override, then confirm it ends at the expiry |
| The install path (venv, its bin directory and the interpreter it points to) is root-owned and not writable by `hostwatch-control` on MediaIn-SVR and TrueNAS-SVR | `namei -l /opt/thermalctl/venv/bin/thermalctl; namei -l $(readlink -f /opt/thermalctl/venv/bin/python)` |
| Override expiry ends on a monotonic deadline recorded when the expiry is first seen, or at `expires_at` by the wall clock, whichever is first, and drops the override if the clocks disagree by more than 60 seconds, so a clock stepped backwards (for example by an NTP correction) cannot extend it | Install an override with a 2 minute `expires_at`, step the clock back 5 minutes with `timedatectl set-time` on a test host, and note that `override ended` is audited within about 2 minutes of install |
| TrueNAS can run the unit (systemd) or needs a Post Init script instead | `systemctl --version` on TrueNAS-SVR |
| Reading `pwmN` back returns the 0 to 255 value last written while `pwmN_enable` is 1 on the nct6775 driver, so the write-only-on-change check (`holds`) sees an external change and does not rewrite every cycle | Supervised test: set manual, write 128 to one header, `cat pwmN` and compare, then `echo 64 > pwmN` from a shell with the service running and watch the service put 128 back within one cycle |
| Skipping repeated identical `pwmN` writes (rewritten every 60 cycles) does not let the chip or firmware drift the duty unseen between reads, and the two minute refresh suits the fans | Supervised test: run active on one header for an hour, log `pwmN` and `fanN_input` once a second and note any change not made by the service |


## Header mapping on MediaIn-SVR (measured 2026-10-03)

The owner ran `thermalctl map-headers --apply`. Each header lowered to 30 percent slowed the fan
with the same number: pwm1 to fan1 (down 823 RPM), pwm2 to fan2 (down 1071 RPM), pwm3 to fan3
(down 923 RPM) and pwm4 to fan4 (down 1744 RPM). Smaller drops on other fans (4 to 156 RPM) are
airflow inside the case. Writing pwmN while pwmN_enable is 5 returned EBUSY on this nct6779,
which confirms that mode 5 is a chip-controlled mode that rejects manual duty writes.
