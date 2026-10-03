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
