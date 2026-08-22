#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Watchdog for a USB-attached UPS whose interface wedges: notice that NUT's data has gone stale, reset the USB device, restart the driver.

Some USB HID UPSes -- CyberPower's are the ones this was written against -- stop answering control transfers while staying enumerated on the
bus. The symptom is distinctive and misleading in equal measure. `lsusb` still lists the device, the kernel logs no disconnect, the device node
keeps its permissions, and NUT's driver still opens and claims interface 0 without complaint. Only the transfers underneath fail:

    [D2] Claimed interface 0 successfully
    [D2] Unable to get HID descriptor (Input/Output Error)
    [D2] Unable to get Report descriptor: Resource temporarily unavailable

From there `upsc` reports `Data stale` and nothing knows the mains state any more, which on a box that is supposed to shut down on a power cut
means the UPS has quietly stopped being a UPS. It can sit like that indefinitely -- the driver logs the same failed rescan every couple of
seconds forever and no unit ever enters a failed state, so nothing that watches systemd will notice.

Restarting the driver does not fix it. A freshly started driver fails in exactly the same place, because the fault is in the device, not in the
process. What does fix it is a USBDEVFS_RESET ioctl on the device node -- the usbfs "re-enumerate this device" call -- after which the identical
driver invocation finds the UPS immediately. So the recovery is reset-plus-restart, in that order, and this runs it from cron.

Silent when there is nothing to say. Nothing is printed when the UPS is healthy, and nothing is printed when it was wedged and the reset worked;
both cases are recorded in syslog instead, so a cron job produces mail only when the UPS is still unreachable after a recovery attempt -- which
is the case that needs a human, and usually means the UPS is unplugged, switched off, or dead rather than merely wedged.

Exit status is 0 when the UPS is talking (whether or not a reset was needed), 1 when it is not, and 2 on a usage error.
"""
import argparse, fcntl, os, re, socket, stat, subprocess, sys, syslog, tempfile, time

USBDEVFS_RESET = (ord("U") << 8) | 20      # _IO('U', 20); usbfs re-enumerates the device, which is what clears the wedged endpoint
SYSFS_USB, DEV_BUS_USB = "/sys/bus/usb/devices", "/dev/bus/usb"


def read_sysfs(directory, name):
    with open(os.path.join(directory, name)) as fh:
        return fh.read().strip()


def find_usb_device(vid, pid):
    """Path of the /dev/bus/usb node for the first device matching vid:pid, or None if it is not on the bus at all.

    Resolved fresh on every run and never configured: bus and device numbers move across reboots, and a reset can renumber the device too."""
    for entry in sorted(os.listdir(SYSFS_USB)):
        d = os.path.join(SYSFS_USB, entry)
        try:
            if (int(read_sysfs(d, "idVendor"), 16), int(read_sysfs(d, "idProduct"), 16)) != (vid, pid):
                continue
            return f"{DEV_BUS_USB}/{int(read_sysfs(d, 'busnum')):03d}/{int(read_sysfs(d, 'devnum')):03d}"
        except (OSError, ValueError):
            continue                       # interfaces and root hubs lack these files; anything unreadable is simply not the device we want
    return None


def validate_device_path(path):
    """Guard for --reset-usb, which is the entry point a sudoers rule grants to an unprivileged user.

    Refuses anything that is not a character device named exactly /dev/bus/usb/BBB/DDD, so that a wildcard in that rule cannot be walked out of
    with `..` and cannot be pointed at an unrelated file. The ioctl alone would fail on a regular file, but the rule should not depend on that."""
    real = os.path.realpath(path)
    parts = real.split("/")
    if len(parts) != 6 or not real.startswith(DEV_BUS_USB + "/") or not all(p.isdigit() and len(p) == 3 for p in parts[4:]):
        sys.exit(f"refusing to reset {path!r}: not a {DEV_BUS_USB}/BBB/DDD node")
    if not stat.S_ISCHR(os.stat(real).st_mode):
        sys.exit(f"refusing to reset {path!r}: not a character device")
    return real


def reset_usb(path):
    """The privileged half: USBDEVFS_RESET on the node. Needs write access, which the udev rules give to NUT's own user and nobody else."""
    fd = os.open(validate_device_path(path), os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)


def read_line(sock):
    chunks = []
    while not chunks or not chunks[-1].endswith(b"\n"):
        if not (chunk := sock.recv(4096)):
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace").split("\n")[0]


def query_status(host, port, ups, timeout):
    """(status, detail) from upsd, speaking the NUT protocol directly. status is None when the UPS is not answering.

    Direct rather than shelling out to upsc so the failure modes stay distinguishable: `ERR DATA-STALE` is the wedge this tool repairs, a refused
    connection means upsd itself is down, and both should read differently in the mail that goes out when recovery fails."""
    try:
        with socket.create_connection((host, port), timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"GET VAR {ups} ups.status\n".encode())
            line = read_line(sock)
            try:
                sock.sendall(b"LOGOUT\n")
            except OSError:
                pass                       # upsd has already answered; a failure to say goodbye politely is not interesting
    except OSError as exc:
        return None, f"cannot reach upsd at {host}:{port}: {exc}"
    if match := re.match(rf'^VAR\s+{re.escape(ups)}\s+ups\.status\s+"(.*)"\s*$', line):
        return match.group(1), line
    return None, line.strip() or "empty response from upsd"


def run(argv, use_sudo):
    """Run a command, prefixing `sudo -n` unless we are already root. Returns (returncode, combined output)."""
    prefix = [] if os.geteuid() == 0 or not use_sudo else ["sudo", "-n"]
    proc = subprocess.run(prefix + argv, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def wait_for_ups(args, deadline):
    """Poll upsd until the driver reports again or the deadline passes. The driver needs a few seconds to walk the report descriptor -- on the
    UPS this was written for, ~10 s to find 76 HID objects -- so an immediate check after `systemctl start` always fails."""
    while True:
        status, detail = query_status(args.host, args.port, args.ups, args.timeout)
        if status is not None or time.monotonic() >= deadline:
            return status, detail
        time.sleep(2)


def recover(args):
    """Reset the device and restart the driver. Returns None on success, or a human-readable reason for the failure."""
    if (path := find_usb_device(args.vid, args.pid)) is None:
        return f"no USB device {args.usb_id} is on the bus at all -- the UPS is unplugged, switched off, or the port is dead"

    steps = [(["systemctl", "stop", args.unit], True),
             ([args.python, os.path.realpath(__file__), "--reset-usb", path], True),
             (["systemctl", "reset-failed", args.unit], False),      # a no-op unless a previous restart left the unit failed
             (["systemctl", "start", args.unit], True)]
    for argv, required in steps:
        rc, output = run(argv, args.sudo)
        if rc and required:
            return f"{' '.join(argv)} failed (rc={rc}): {output or 'no output'}"

    status, detail = wait_for_ups(args, time.monotonic() + args.startup_timeout)
    return None if status is not None else f"UPS still not answering {args.startup_timeout}s after the reset: {detail}"


def main():
    parser = argparse.ArgumentParser(description="Reset a wedged USB UPS and restart its NUT driver. Silent unless recovery fails.")
    parser.add_argument("--ups", default="ups", help="UPS name as configured in ups.conf (default: ups)")
    parser.add_argument("--host", default="127.0.0.1", help="upsd address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3493, help="upsd port (default: 3493)")
    parser.add_argument("--usb-id", default="0764:0501", help="vendor:product of the UPS, hex (default: 0764:0501, CyberPower)")
    parser.add_argument("--unit", default="nut-driver@ups", help="systemd unit for the driver (default: nut-driver@ups)")
    parser.add_argument("--retries", type=int, default=2, help="confirmations that the UPS is really wedged before resetting (default: 2)")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="seconds between those confirmations (default: 5)")
    parser.add_argument("--timeout", type=float, default=5.0, help="upsd socket timeout in seconds (default: 5)")
    parser.add_argument("--startup-timeout", type=float, default=60.0, help="seconds to wait for the driver after a reset (default: 60)")
    parser.add_argument("--python", default="/usr/bin/python3", help="interpreter for the privileged --reset-usb re-invocation")
    parser.add_argument("--no-sudo", dest="sudo", action="store_false", help="never prefix sudo, even when not root")
    parser.add_argument("--lock", help="lock file (default: one per UPS name under the temporary directory)")
    parser.add_argument("--verbose", action="store_true", help="also report the healthy case, for running by hand")
    parser.add_argument("--reset-usb", metavar="PATH", help=argparse.SUPPRESS)     # privileged entry point; see the sudoers rule in the README
    args = parser.parse_args()

    if args.reset_usb:                     # this is the whole job when invoked through sudo; nothing else here needs privileges
        reset_usb(args.reset_usb)
        return 0

    try:
        args.vid, args.pid = (int(part, 16) for part in args.usb_id.split(":", 1))
    except ValueError:
        parser.error(f"--usb-id must be hex vendor:product, e.g. 0764:0501 (got {args.usb_id!r})")

    lock = args.lock or os.path.join(tempfile.gettempdir(), f"nut-usb-watchdog-{args.ups}-{os.geteuid()}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                           # a previous run is still recovering; saying so every minute would be noise, not information

    syslog.openlog("nut-usb-watchdog", syslog.LOG_PID, syslog.LOG_DAEMON)
    for attempt in range(args.retries + 1):
        status, detail = query_status(args.host, args.port, args.ups, args.timeout)
        if status is not None:
            if args.verbose:
                print(f"{args.ups}: {status}")
            return 0
        if attempt < args.retries:
            time.sleep(args.retry_delay)

    syslog.syslog(syslog.LOG_WARNING, f"{args.ups} not answering ({detail}); resetting {args.usb_id} and restarting {args.unit}")
    if (failure := recover(args)) is None:
        syslog.syslog(syslog.LOG_NOTICE, f"{args.ups} recovered by USB reset")
        if args.verbose:
            print(f"{args.ups}: recovered by USB reset")
        return 0

    syslog.syslog(syslog.LOG_ERR, f"{args.ups} recovery failed: {failure}")
    print(f"nut-usb-watchdog: {args.ups} is not being monitored and the reset did not fix it.\n  {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
