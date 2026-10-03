# thermal-control-linux: working contract

Read this file first. `docs/ARCHITECTURE.md` holds the design and `UNVERIFIED.md` the
open hardware assumptions.

## What this project is

A small root service per Linux host that sets fan speeds from a load-aware curve: fans
stay slow while the host is cool and idle and rise to full speed as temperature or load
climbs. It is the Linux and Raspberry Pi counterpart of Thermal Control Suite
(`~/repos/thermal-control-suite`, Windows). hostwatch (`~/repos/hostwatch`) only watches
it by reading the status file this service writes; hostwatch never writes to hardware.

## Rules

1. **Fail safe first.** On any error, crash, lost or stale sensor, stalled fan, invalid
   config, or exit, fans go to full speed or back to firmware control. A change that can
   leave a fan slower than it should be is a defect, not a trade-off.
2. **Dry run is the default.** The service computes and logs, and writes to hardware only
   when the config enables it for a header that has passed its mapping test.
3. **Never state a hardware fact you have not measured.** sysfs paths, pwm_enable values,
   header mappings and driver behaviour go in `UNVERIFIED.md` with the confirming command
   until measured on the host.
4. **Run `pytest` before calling any change done.** Hardware is reached only through a
   backend interface; tests use fakes and fake sysfs trees.
5. **Audit every change** of target, curve, mode or enabled header with old and new values.
6. **House style for committed files:** complete sentences, explain why as well as what,
   no em dashes, no attribution footers or generation notices, no model names.

## Target hosts

| Host | Fan hardware | Notes |
|---|---|---|
| MediaIn-SVR | NCT6779D via nct6775, pwm1 to pwm5 | All fans 4-pin (owner, 2026-10-03). Headers not yet mapped. |
| TrueNAS-SVR | Likely Nuvoton NCT679x via nct6775, driver not loaded | Owner accepts `acpi_enforce_resources=lax`. Persist through a Post Init script. All fans 4-pin. |
| ai-pi | Pi 5 fan through kernel trip points | Configure trip points, do not run a userspace loop. |
