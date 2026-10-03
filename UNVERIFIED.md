# Unverified assumptions

Each row is believed but not measured. Remove a row only after measuring it, and say
how in the commit message.

| Assumption | Verify with |
|---|---|
| MediaIn-SVR exposes pwm1 to pwm5 with pwmN_enable through nct6775 | `ls /sys/class/hwmon/*/pwm*; for f in /sys/class/hwmon/*/pwm*_enable; do echo $f $(cat $f); done` |
| pwmN_enable 1 is manual on nct6775 and 0 is full speed | Driver documentation plus a supervised test on one header |
| TrueNAS-SVR has a Nuvoton NCT679x chip that nct6775 binds | `cat /sys/class/dmi/id/board_name; sudo modprobe nct6775; ls /sys/class/hwmon/*/name; sudo dmesg \| grep -i -E 'nct\|resource conflict'` |
| The Pi 5 fan is controlled by kernel trip points set in config.txt | `ls /sys/class/thermal/cooling_device*; cat /sys/class/thermal/cooling_device*/type; grep -i fan /boot/firmware/config.txt` |
| The placeholder sensor and header paths in `docs/example.toml` (hwmon numbering, `temp1_input`, `pwm1`, `pwm2`) match MediaIn-SVR | `for d in /sys/class/hwmon/hwmon*; do echo $d $(cat $d/name); done; ls /sys/class/hwmon/*/temp*_input /sys/class/hwmon/*/pwm?` |
| The example curve points and floors (min duty 20, min RPM 300, stall window 15 s) suit the installed 4-pin fans | Supervised step test per header while watching `fanN_input` and temperatures |
| The default fail-safe hold period, hysteresis, ramp-down rate and EMA alpha suit the installed fans and sensors | Supervised step test per header while watching temperatures and `fanN_input` for hunting or slow recovery |
| Writing 100 percent duty (or releasing to firmware) on every enabled header is the correct response to a failed cycle on the real fans and driver | Supervised test: make a read fail on MediaIn-SVR and watch `fanN_input` rise |
| The controller's default cycle interval and status path `/run/thermalctl/status.json` suit hostwatch and the systemd `RuntimeDirectory` | Check the unit once written and hostwatch's configured source path |
| The meanings of nct6775 `pwmN_enable` values used by the sysfs backend (1 manual, other values such as 5 for the firmware smart mode, and which value means full speed) and that restoring the recorded value returns control to firmware | Kernel hwmon sysfs-interface documentation for the driver, then on one supervised header: record `cat pwmN_enable`, set 1, write pwmN, restore, and watch `fanN_input` |
| Writing 255 to pwmN gives full fan speed on the 4-pin fans after a failed restore, and the fan reads the pwm file as 0 to 255 | Supervised test: set manual, write 255 and 0 to one header, watch `fanN_input` |
| The unit's install paths (`/opt/thermalctl/venv/bin/thermalctl`, `/etc/thermalctl/config.toml`), `Type=notify` with `NotifyAccess=main`, `WatchdogSec=30` and `RuntimeDirectoryPreserve=yes` behave as intended on Debian and TrueNAS | `systemd-analyze verify /etc/systemd/system/thermalctl.service`, then `systemctl start thermalctl; systemctl show thermalctl -p WatchdogUSec,NotifyAccess`, then `kill -9` the main process and confirm `pwmN_enable` returns to its recorded value |
| `/proc/stat` utilization over one cycle is a suitable load input, and the 2 second cycle default suits the fans | Compare `thermalctl status --json` load against `top` under a known load |
| Lowering to 30 percent and an 8 second settle shows a clear RPM drop on the installed 4-pin fans during `map-headers --apply`, and fan inputs sit in the same hwmon directory as the pwm files | Run `map-headers --apply` on one supervised header and compare against `ls /sys/class/hwmon/*/fan*_input` |
| TrueNAS can run the unit (systemd) or needs a Post Init script instead | `systemctl --version` on TrueNAS-SVR |

