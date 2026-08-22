# nut-usb-watchdog

Notice that a USB-attached UPS has stopped talking to [NUT](https://networkupstools.org/), reset the USB device, and restart the driver.
Silent unless the reset did not work, so it can go straight in a crontab.

```bash
nut-usb-watchdog.py --usb-id 0764:0501 --unit nut-driver@ups
```

## The failure it repairs

Some USB HID UPSes — CyberPower's are the ones this was written against — stop answering control transfers while staying enumerated on the bus.

Everything you would normally check says the device is fine.
`lsusb` still lists it, the kernel logs no disconnect and no re-enumeration, the device node keeps its owner and mode, and NUT's driver still opens it and claims interface 0 without complaint.
Only the transfers underneath fail:

```
[D2] Claimed interface 0 successfully
[D2] Unable to get HID descriptor (Input/Output Error)
[D2] Unable to get Report descriptor: Resource temporarily unavailable
```

From there `upsc` reports `Data stale`, nothing knows the mains state any more, and a box that was supposed to shut down cleanly on a power cut no longer will.

Two things make this worth a watchdog rather than a note in a runbook.

**Nothing raises an alarm.** The driver does not exit — it logs the same failed rescan every couple of seconds indefinitely, and its systemd unit stays `active`. So nothing that watches units, and nothing that watches for a crash, will ever notice. The UPS can sit unmonitored for months.

**Restarting the driver does not fix it.** A freshly started driver fails in exactly the same place, because the fault is in the device, not in the process. What clears it is a `USBDEVFS_RESET` ioctl on the device node — the usbfs "re-enumerate this device" call — after which the identical driver invocation finds the UPS immediately. So the recovery is reset *then* restart, in that order, which is what this runs.

The driver flags usually suggested for a CyberPower that stops answering (`pollonly`, `pollinterval`, `usb_set_altinterface`) do not help, because they change polling behaviour and communication is already dead at the descriptor level before any polling happens.

## What it does

1. Asks `upsd` for `ups.status`, speaking the NUT protocol directly rather than shelling out to `upsc`, so that a stale driver and an absent `upsd` stay distinguishable in the mail that goes out.
2. If the UPS answers, exits 0 and prints nothing.
3. Otherwise confirms it (`--retries`, default two more checks five seconds apart) so a momentarily busy `upsd` does not trigger a reset.
4. Finds the device by vendor:product in `/sys`, stops the driver unit, resets the device, and starts the unit again.
   Bus and device numbers are resolved fresh every run and never configured, because they move across reboots and a reset can renumber the device too.
5. Waits up to `--startup-timeout` for the driver to report — it needs a few seconds to walk the report descriptor — then re-checks.

Exit status is 0 when the UPS is talking (whether or not a reset was needed), 1 when it is not, and 2 on a usage error.

## Output, and why there is so little of it

Nothing is printed when the UPS is healthy, and nothing is printed when it was wedged and the reset worked.
Both are recorded in syslog instead:

```
nut-usb-watchdog[2891]: ups not answering (ERR DATA-STALE); resetting 0764:0501 and restarting nut-driver@ups
nut-usb-watchdog[2891]: ups recovered by USB reset
```

So a cron job produces mail only for the case that actually needs a person — the UPS still unreachable after a reset, which usually means it is unplugged, switched off, or dead rather than merely wedged:

```
nut-usb-watchdog: ups is not being monitored and the reset did not fix it.
  no USB device 0764:0501 is on the bus at all -- the UPS is unplugged, switched off, or the port is dead
```

`--verbose` reports the healthy and recovered cases too, for running by hand.
Concurrent runs are serialised with a lock file and a second one exits silently, so a short cron interval cannot stack up recoveries.

## Privileges

Resetting the device needs write access to its `/dev/bus/usb` node, which udev gives to NUT's own user and nobody else, and restarting the unit needs systemd.
Run as root and neither is a problem — the tool skips `sudo` when it is already root.

Run as an ordinary user and it calls `sudo -n` for exactly four commands, which is all the sudoers rule needs to grant:

```sudoers
# /etc/sudoers.d/nut-usb-watchdog — install mode 0440, and check with `visudo -cf` before you trust it
Cmnd_Alias NUT_USB_WATCHDOG = /usr/bin/systemctl stop nut-driver@ups, \
                              /usr/bin/systemctl start nut-driver@ups, \
                              /usr/bin/systemctl reset-failed nut-driver@ups, \
                              /usr/bin/python3 /opt/nut-usb-watchdog/nut-usb-watchdog.py --reset-usb /dev/bus/usb/[0-9][0-9][0-9]/[0-9][0-9][0-9]

youruser ALL=(root) NOPASSWD: NUT_USB_WATCHDOG
```

Four things about that rule are deliberate:

- **The device path is a character class, not `*`.** A `*` in sudoers matches `/` as well, so `/dev/bus/usb/*` would also permit `/dev/bus/usb/../../../etc/shadow`. `[0-9][0-9][0-9]` cannot leave the directory. The tool independently refuses any path that is not a character device named exactly `/dev/bus/usb/BBB/DDD`, so the rule does not have to be the only guard — but it should still be a correct one.
- **`--reset-usb` is a separate entry point** that does the ioctl and nothing else, so the rule grants that one operation rather than "run this program as root".
- **The interpreter is named explicitly**, and must match the tool's `--python` (default `/usr/bin/python3`). Under `uv run` the outer interpreter is a uv-managed build whose path is neither stable nor predictable, so the privileged re-invocation deliberately uses the system Python instead. The tool is standard library only, so it runs correctly under either.
- **The script must not be writable by the user the rule grants to**, or the grant is a root escalation: they could rewrite the file the rule points at. Install it root-owned, and put it somewhere the user cannot replace — which means a path under that user's home directory is the wrong place for it unless they already have full root anyway.

## Running it from cron

```crontab
*/10 * * * * /opt/nut-usb-watchdog/nut-usb-watchdog.py --usb-id 0764:0501 --unit nut-driver@ups
```

Ten minutes is a reasonable default: the wedge is not urgent to catch — the UPS keeps powering the machine either way, it just stops being able to say so — but the window between the wedge and the next power cut is exactly the exposure.

Choose the interval against the machine's own shutdown timings, not against how quickly you would like to know: what matters is that the driver is back before `battery.runtime` matters, and a UPS that holds the load for hours makes a ten-minute check generous.

## Requirements

Python 3.12+, standard library only.
Runs under [uv](https://docs.astral.sh/uv/) via the PEP 723 header (`./nut-usb-watchdog.py`), or under any system Python (`python3 nut-usb-watchdog.py`) where uv is not installed.

Linux only — it depends on `/sys/bus/usb`, `/dev/bus/usb` and the usbfs `USBDEVFS_RESET` ioctl.

## Options

| Option | Default | |
|---|---|---|
| `--ups` | `ups` | UPS name as configured in `ups.conf` |
| `--host` / `--port` | `127.0.0.1` / `3493` | where `upsd` listens |
| `--usb-id` | `0764:0501` | vendor:product in hex, from `lsusb` |
| `--unit` | `nut-driver@ups` | systemd unit for the driver |
| `--retries` / `--retry-delay` | `2` / `5.0` | confirmations that it is really wedged, and the gap between them |
| `--timeout` | `5.0` | `upsd` socket timeout |
| `--startup-timeout` | `60.0` | how long to wait for the driver after a reset |
| `--python` | `/usr/bin/python3` | interpreter for the privileged re-invocation; must match the sudoers rule |
| `--no-sudo` | | never prefix `sudo`, even when not root |
| `--lock` | per UPS name and uid, in the temporary directory | lock file |
| `--verbose` | | also report the healthy and recovered cases |
