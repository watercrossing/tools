#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Backfill Apache/nginx "combined" access logs into GoatCounter, without collapsing visitor sessions.

GoatCounter marks a pageview as a *visit* (first_visit=1) by looking up a session for (User-Agent + IP) and checking whether that session has
already seen the path. Sessions expire after 8 hours of **wall-clock** inactivity -- not 8 hours of log time. So `goatcounter import`, which
replays a logfile as fast as it can, puts an entire archive inside one session window: a visitor who returned daily to the same page collapses
into ONE visit stamped at their first ever hit, and every later day gets no row at all. Measured, not guessed: a 3-line log of one visitor
hitting /probe on three different days imported as a single visit on day one.

The /api/v0/count API accepts an explicit `session` per hit, which takes precedence over the UA+IP hashing (handlers/api.go: `case a.Session
!= ""`). This tool assigns sessions from *log* time -- a new session whenever a given (IP, User-Agent) has been idle more than 8 hours -- which
reproduces GoatCounter's real semantics over historical data. IP is still sent so GeoIP still works; `session` simply wins over it.
"""
import argparse, gzip, json, os, re, sys, time, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone

# 1.2.3.4 - - [30/Jul/2026:10:45:40 +0100] "GET /path?q=1 HTTP/1.1" 200 27338 "-" "Mozilla/5.0 ..."
LINE_RE = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z_]+) (?P<url>\S*) (?P<proto>[^"]*)" '
                     r'(?P<status>\d{3}) (?P<size>\S+) "(?P<ref>(?:[^"\\]|\\.)*)" "(?P<ua>(?:[^"\\]|\\.)*)"')
STATIC_RE = re.compile(r'.*\.(?:js|css|gif|jpe?g|png|svg|ico|web[mp]|mp[34])$', re.I)  # "static" as GoatCounter expands it
SESSION_IDLE = timedelta(hours=8)  # GoatCounter's memstore.SessionTime
MAX_FIELD = 2000                   # GoatCounter rejects ref/path over 2048 chars
BATCH = 500


def parse_ts(s):
    return datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z")


def opener(path):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")


def first_ts(path):
    """First parseable timestamp in a file, so multiple rotations can be replayed in chronological order."""
    with opener(path) as fh:
        stamps = (parse_ts(m.group("ts")) for m in map(LINE_RE.match, (fh.readline() for _ in range(200))) if m)
        return next(stamps, datetime.max.replace(tzinfo=timezone.utc))


class Sender:
    """Batches hits to /api/v0/count. `host` sets the Host header, which is how GoatCounter picks the site -- so you can POST straight at the
    backend on localhost and name the site explicitly, skipping the reverse proxy and a TLS handshake per batch."""

    def __init__(self, url, token, host=None, dry_run=False, rate=0.0):
        self.url, self.token, self.host, self.dry_run, self.rate = url.rstrip("/") + "/api/v0/count", token, host, dry_run, rate
        self.sent = self.batches = self.rejected = 0

    def send(self, hits):
        if not hits:
            return
        if self.dry_run:
            self.sent, self.batches = self.sent + len(hits), self.batches + 1
            return
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.token} | ({"Host": self.host} if self.host else {})
        req = urllib.request.Request(self.url, data=json.dumps({"hits": hits}).encode(), method="POST", headers=headers)
        for attempt in range(8):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                self.sent, self.batches = self.sent + len(hits), self.batches + 1
                time.sleep(self.rate) if self.rate else None
                return
            except urllib.error.HTTPError as e:
                payload = e.read()[:2000].decode(errors="replace")
                if e.code in (429, 502, 503, 504):
                    wait = min(2 ** attempt, 60)
                    print(f"  [{e.code}] backing off {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                # A 400 carries a per-hit {"errors": {"<index>": "..."}} map, and GoatCounter still records every OTHER hit in the batch.
                # Aborting here would make the run non-resumable without double-counting, so note it and move on.
                if e.code == 400 and '"errors"' in payload:
                    bad = json.loads(payload).get("errors", {}) if payload.lstrip().startswith("{") else {}
                    for k, v in list(bad.items())[:3]:
                        print(f"  [400] hit {k} rejected: {v}", file=sys.stderr)
                    self.rejected += len(bad) or 1
                    self.sent, self.batches = self.sent + len(hits) - (len(bad) or 1), self.batches + 1
                    return
                raise SystemExit(f"API error {e.code}: {payload}")
            except urllib.error.URLError as e:
                wait = min(2 ** attempt, 60)
                print(f"  [conn {e.reason}] retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
        raise SystemExit("giving up after repeated API failures")


def excluded(path, status, drop_redirect, drop_static, drop_4xx, path_res):
    return ((drop_redirect and status in (300, 301, 302, 303)) or (drop_4xx and 400 <= status < 500)
            or (drop_static and STATIC_RE.match(path)) or any(r.match(path) for r in path_res) or len(path) > MAX_FIELD)


def run(files, sender, since, until, skip, label, **excl):
    sessions, hits, nth = {}, [], 0                       # sessions: (ip, ua) -> [session_id, last_seen]
    stats = dict(skipped=0, malformed=0, out_of_range=0)
    last = None

    for path in sorted(files, key=first_ts):
        with opener(path) as fh:
            for line in fh:
                if not (m := LINE_RE.match(line)):
                    stats["malformed"] += line.strip() != ""
                    continue
                ts = parse_ts(m.group("ts"))
                if (since and ts <= since) or (until and ts >= until):
                    stats["out_of_range"] += 1
                    continue
                p, _, q = m.group("url").partition("?")
                p = p if p.startswith("/") else "/" + p.lstrip("/")
                if excluded(p, int(m.group("status")), **excl):
                    stats["skipped"] += 1
                    continue

                key = (m.group("ip"), m.group("ua"))
                if (s := sessions.get(key)) is None or ts - s[1] > SESSION_IDLE:
                    sessions[key] = [uuid.uuid4().hex, ts]  # idle too long (in log time) -> a new visit
                else:
                    s[1] = ts

                nth += 1
                if nth <= skip:                            # already sent by an earlier run
                    continue
                hit = {"path": p, "ip": key[0], "user_agent": key[1], "session": sessions[key][0],
                       "created_at": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                hits += [hit | {k: v for k, v in (("query", q), ("ref", m.group("ref")[:MAX_FIELD] if m.group("ref") != "-" else "")) if v}]
                last = ts

                if len(hits) >= BATCH:
                    sender.send(hits)
                    hits = []
                    if sender.batches % 40 == 0:
                        print(f"  {label}: {sender.sent:>8} sent  (at {ts:%Y-%m-%d})", flush=True)
                if len(sessions) > 400_000:                # bound memory on multi-year runs
                    sessions = {k: v for k, v in sessions.items() if v[1] > ts - SESSION_IDLE}

    sender.send(hits)
    print(f"{label}: sent={sender.sent} rejected={sender.rejected} "
          + " ".join(f"{k}={v}" for k, v in stats.items()) + f" last={last}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, help="where to POST, e.g. https://stats.example.com or http://127.0.0.1:8082")
    ap.add_argument("--host", help="Host header naming the GoatCounter site; needed when --site is an IP/port")
    ap.add_argument("--label", help="prefix for progress lines (default: --host or --site)")
    ap.add_argument("--since", help="only hits strictly after this ISO8601 instant")
    ap.add_argument("--until", help="only hits strictly before this ISO8601 instant")
    ap.add_argument("--exclude-redirect", action="store_true", help="drop 30[0123] responses")
    ap.add_argument("--exclude-static", action="store_true", help="drop js/css/image/media requests")
    ap.add_argument("--exclude-4xx", action="store_true", help="drop 4xx; removes most vulnerability-scanner noise")
    ap.add_argument("--exclude-path-re", action="append", default=[], metavar="RE", help="drop paths matching (anchored at start); repeatable")
    ap.add_argument("--skip", type=int, default=0, help="skip the first N qualifying hits, to resume an interrupted run")
    ap.add_argument("--rate", type=float, default=0.0, help="seconds to sleep between batches")
    ap.add_argument("--dry-run", action="store_true", help="parse and count, send nothing")
    ap.add_argument("files", nargs="+", help="access logs, plain or .gz; replayed in chronological order")
    a = ap.parse_args()

    if not (token := os.environ.get("GOATCOUNTER_API_KEY", "")) and not a.dry_run:
        raise SystemExit("GOATCOUNTER_API_KEY is not set (needs the 'Record pageviews' permission)")
    if not (files := [f for f in a.files if os.path.isfile(f) and os.path.getsize(f)]):
        raise SystemExit("no non-empty input files")

    run(files, Sender(a.site, token, a.host, a.dry_run, a.rate),
        datetime.fromisoformat(a.since) if a.since else None, datetime.fromisoformat(a.until) if a.until else None,
        a.skip, a.label or a.host or a.site, drop_redirect=a.exclude_redirect, drop_static=a.exclude_static, drop_4xx=a.exclude_4xx,
        path_res=[re.compile(r) for r in a.exclude_path_re])


if __name__ == "__main__":
    main()
