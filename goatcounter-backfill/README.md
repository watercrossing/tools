# goatcounter-backfill

Replays archived Apache/nginx `combined` access logs into [GoatCounter](https://www.goatcounter.com/) without collapsing visitor sessions.

## Why not just `goatcounter import`?

GoatCounter has a perfectly good `goatcounter import -format=combined` for exactly this.
Use it for *following* a live log (`-follow`).
Do not use it to replay an archive, because the numbers it produces are wrong in a way that is easy to miss.

GoatCounter marks a pageview as a *visit* (`first_visit=1`) by looking up a session for (User-Agent + IP) and checking whether that session has already seen the path.
Sessions expire after **8 hours of wall-clock inactivity** — not 8 hours of log time.
`goatcounter import` replays a file as fast as it can, so an entire archive lands inside a single session window.

The effect, measured rather than assumed — a three-line log of one visitor hitting `/probe` on three different days:

| | rows written |
|---|---|
| `goatcounter import` | **1** visit, stamped on day one; days two and three produce no row at all |
| this tool | 3 visits, one per day |

It is not merely an undercount: the later days are *absent*, so a year of history imports as one spike and a flat line.

The fix is that `/api/v0/count` accepts an explicit `session` string per hit, which takes precedence over GoatCounter's UA+IP hashing (`handlers/api.go`: `case a.Session != ""`).
This tool assigns sessions from **log** time — a new session whenever a given (IP, User-Agent) pair has been idle for more than 8 hours — reproducing GoatCounter's real semantics over historical data.
The IP is still sent so GeoIP lookups still work; the explicit session simply wins over it.

## Usage

```sh
export GOATCOUNTER_API_KEY=...          # needs the "Record pageviews" permission
uv run goatcounter-backfill.py --site https://stats.example.com \
  --since 2025-02-05T16:04:31+00:00 --until 2026-07-28T10:45:00+01:00 \
  --exclude-redirect --exclude-static \
  /var/log/httpd/example-ssl-access.log*
```

Pure standard library; `uv` only supplies the Python version.
Files are replayed in chronological order (by each file's first timestamp), so you can pass a whole glob of rotations at once.
`.gz` logs are read transparently.

Start with `--dry-run` — it parses and counts without sending anything, which is the cheap way to sanity-check your date range and excludes.

| Option | |
|---|---|
| `--site` | where to POST, e.g. `https://stats.example.com` or `http://127.0.0.1:8082` |
| `--host` | `Host` header naming the GoatCounter site; needed when `--site` is an IP/port |
| `--since` / `--until` | ISO8601 bounds, exclusive; how you avoid re-importing what is already there |
| `--exclude-redirect` | drop `30[0123]` |
| `--exclude-static` | drop js/css/image/media |
| `--exclude-4xx` | drop 4xx — removes most vulnerability-scanner noise |
| `--exclude-path-re` | drop paths matching a regex, anchored at the start; repeatable |
| `--skip N` | skip the first N qualifying hits, to resume an interrupted run |
| `--rate` | seconds to sleep between batches |

GoatCounter picks the **site from the `Host` header**, so with `--host` you can POST straight at the backend and skip the reverse proxy and a TLS handshake per batch:

```sh
uv run goatcounter-backfill.py --site http://127.0.0.1:8082 --host stats.example.com ...
```

Mirror whatever `-exclude` flags the live importer uses, so backfilled data is filtered identically to data collected going forward — otherwise the join between old and new is visible as a step in the graphs.

## Things that will bite you

**The rate limit.**
`api-count` defaults to 60 requests / 2 minutes, which is days for a large archive.
Raise it for the duration and put it back afterwards:

```sh
goatcounter serve -ratelimit api-count:10000/1 ...
```

**Over-long referrers.**
A scanner will eventually send a `Referer` above GoatCounter's 2048-char limit, which fails the whole batch with `ref: must be shorter than 2048 characters`.
This tool truncates such fields, and treats a per-hit `400` as non-fatal because GoatCounter still records every *other* hit in the batch — aborting there would leave you unable to resume without double-counting.

**Resuming.**
Batches are 500 hits, and the `400` body names the failing index within the batch, so an interrupted run has an exactly computable resume point: `--skip ((batch_index + 1) * 500)`.
Parsing is deterministic, so the same inputs and flags always yield the same hit ordering.

**`hit_counts` is additive.**
Backfilling into an hour that already holds data adds to it rather than replacing it — verified before relying on it.
Good (no clobbering), but it also means importing the same range twice silently doubles it.
Get `--since` / `--until` right.

**Bot traffic still counts.**
GoatCounter filters obvious bot User-Agents itself, but scanners probing `/wp-admin/…` present browser-like UAs.
`--exclude-4xx` is the effective lever — on one real vhost it removed ~92% of lines.

## Verifying a run

Check the two behaviours you care about against a throwaway instance before touching real data:

```sh
goatcounter db create site -createdb -db sqlite+/tmp/t.sqlite3 -vhost gc.test -user.email t@example.com -user.password testtesttest
goatcounter serve -db sqlite+/tmp/t.sqlite3 -listen localhost:8099 -tls http
```

Feed it a handful of synthetic lines and check that repeat visits on different days count separately while repeats within 8 hours collapse to one.
That is the whole contract of this tool, and it takes a minute to confirm.
