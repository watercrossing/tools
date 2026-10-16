#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""slaac-prefix-watch — log which global IPv6 prefixes this host holds, to see how often the ISP renumbers.

Meant for a box behind a consumer router that delegates a dynamic prefix and announces a /64 of it to the LAN:
the router itself usually can't be asked, but every host's SLAAC addresses carry the prefix and its lifetimes.

Silent on success; prints (so cron mails) only when the set of preferred prefixes changes or `ip` fails.
Stdlib only, and kept to Python 3.10, so cron can run it with the system python3 on hosts without uv.
"""
import ipaddress
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "slaac-prefix-watch" / "slaac-prefix-watch.jsonl"
GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")  # excludes the ULAs (fc00::/7), which routers also announce and which never move


def fetch():
    out = subprocess.run(["ip", "-j", "-6", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=30, check=True).stdout
    prefixes = {}
    for link in json.loads(out):
        # Privacy (temporary) addresses rotate on their own schedule but always share the prefix of a stable address, so skip them.
        for a in link.get("addr_info", []):
            if "local" not in a or a.get("temporary"):
                continue
            iface = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
            if iface.ip not in GLOBAL_UNICAST:
                continue
            # When the router renumbers it keeps announcing the old prefix with preferred=0 until it expires: "deprecated".
            prefixes[str(iface.network)] = {"prefix": str(iface.network), "dev": link["ifname"], "preferred": a.get("preferred_life_time"),
                                            "valid": a.get("valid_life_time"), "deprecated": bool(a.get("deprecated"))}
    return sorted(prefixes.values(), key=lambda p: p["prefix"])


def live(record):
    return sorted(p["prefix"] for p in record.get("prefixes", []) if not p["deprecated"])


def read_log():
    return [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()] if LOG.exists() else []


def append(record):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def sample():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        record = {"time": now, "prefixes": fetch()}
    except Exception as e:
        append({"time": now, "error": str(e)})
        sys.exit(f"slaac-prefix-watch: failed to read addresses: {e}")
    previous = next((r for r in reversed(read_log()) if "error" not in r), None)
    append(record)
    if previous is not None and live(previous) != live(record):
        print(f"slaac-prefix-watch: prefixes changed {live(previous) or 'none'} -> {live(record) or 'none'} at {now}")


def report():
    records = read_log()
    samples = [r for r in records if "error" not in r]
    errors = len(records) - len(samples)
    if not samples:
        print(f"no successful sample yet ({errors} failed) in {LOG}")
        return
    changes = [(b["time"], live(a), live(b)) for a, b in zip(samples, samples[1:]) if live(a) != live(b)]
    print(f"{len(samples)} sample(s) ({errors} failed) from {samples[0]['time']} to {samples[-1]['time']}")
    print(f"current: {', '.join(live(samples[-1])) or 'no global prefix'}, held since {changes[-1][0] if changes else samples[0]['time']}")
    print(f"{len(changes)} change(s):" if changes else "no changes observed")
    for t, old, new in changes:
        print(f"  {t}  {', '.join(old) or 'none'} -> {', '.join(new) or 'none'}")


if __name__ == "__main__":
    report() if sys.argv[1:] == ["--report"] else sample()
