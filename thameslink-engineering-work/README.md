# thameslink-engineering-work

Turns [Thameslink's planned engineering work page](https://www.thameslinkrailway.com/service-updates/planned-engineering-work) into a single `.ics` calendar you can import into Google Calendar (or any other calendar app).

## Why

The page only shows one day at a time (`?date=YYYY-MM-DD`), with no overview and no filtering by route.
Engineering work is planned weeks ahead, so the only way to see what's coming up is to click through every day by hand.
This fetches N consecutive days, extracts each engineering-work item, and writes one calendar event per item — covering the whole Thameslink network, not just any one route.

## Usage

```sh
uv run thameslink-engineering-work.py                      # next 60 days, everything, -> thameslink-engineering-work.ics
uv run thameslink-engineering-work.py --days 90 -o out.ics
uv run thameslink-engineering-work.py --filter "Gatwick Airport" --filter "Bedford"
```

| Option | |
|---|---|
| `--start-date` | first date to check, `YYYY-MM-DD` (default: today) |
| `--days` | how many consecutive days to check (default: 60) |
| `-o`, `--output` | output `.ics` path (default: `thameslink-engineering-work.ics`) |
| `--filter TEXT` | only keep events whose title, routes or description contain `TEXT` (case-insensitive); repeatable, matches are OR'd. Default: keep everything |
| `--llm-verify` | re-check each `--filter` match by shelling out to `claude -p`, dropping ones that only name a station as the unaffected origin/destination of a service altered elsewhere. Requires `--filter` and one of the two options below |
| `--llm-system-prompt TEXT` | the system prompt describing your route, for `--llm-verify` (which stations, in what order, and what kinds of alteration don't matter to you — see `--llm-verify`'s section below) |
| `--llm-system-prompt-file PATH` | same, read from a file instead |
| `--llm-model NAME` | model passed to `claude -p --model` (default: `sonnet`) |
| `--llm-effort LEVEL` | effort level passed to `claude -p --effort` (default: `high`) |
| `--llm-verify-debug` | with `--llm-verify`, print each item's full `claude -p` response (its reasoning included) to stderr |
| `--llm-cache PATH` | with `--llm-verify`, cache verdicts here across runs (default: `llm-verify-cache.local.json` next to the script) |
| `--no-llm-cache` | with `--llm-verify`, don't read or write `--llm-cache`, always re-verify |
| `--delay` | seconds between requests (default: 0.5) |
| `-q`, `--quiet` | suppress per-day progress on stderr |

Then in Google Calendar: **Settings → Import & export → Import**, pick the `.ics` file, and choose which calendar to add it to.
Re-running the tool regenerates the whole file from scratch — Google's import doesn't reliably dedupe by UID, so re-importing after a rerun is liable to create duplicates unless you clear out the old events first (a dedicated calendar for this makes that easy).

## How it works

The page is server-rendered, not client-side: each engineering-work item is already in the HTML as an accordion entry, with a stable id like `id="INC070938BFB2984BC083EFF366FC5D017F"`.
That id is what makes de-duplication possible — a closure spanning a weekend appears on every day of its own range, and this tool keeps just one copy per id.
The same accordion markup is reused for unrelated nav/footer sections (`id="Travel-information"`, `id="Contact-us"`, …), so items are only kept if their id starts `INC`.

Each item's `<p><strong>Date:</strong>...` field gives a range like `26 September - 27 September 2026 11:59 PM` — the year is only written once, against the end date.
If the range crosses a calendar year boundary (`30 December - 2 January 2027`), the start is assumed to be the year before the end.
Events are written as **all-day**, spanning the given date range; the actual overnight timing (e.g. "from end of Saturday service until 08:30") is in the free-text description, since it varies per item and isn't worth trying to parse out.

The site 403s a plain `requests`/`curl` User-Agent (confirmed: identical request with a browser User-Agent gets `200`, a descriptive tool UA still gets `403`), so this sends a browser-like one.
`robots.txt` doesn't disallow this page, and the tool pauses `--delay` seconds (default 0.5s) between the ~60 requests a full run makes.

## `--filter`'s blind spot, and what `--llm-verify` does about it

`--filter` is a plain substring match over an item's title, routes and description, so it can't tell "this station is where the disruption is" from "this station is just the normal, unaffected origin of a service whose alteration happens somewhere else further down the line".
A late-night amendment that starts at one of your stations and simply terminates early further down the route matches on that station name, even though your actual journey is untouched.

`--llm-verify` re-checks each item that has *already passed* `--filter` by shelling out to `claude -p` (the Claude Code CLI — needs to already be installed and logged in; no separate API key required), and drops the ones it judges as that kind of incidental mention — it never sees, and never costs anything for, an item `--filter` has already discarded.
It needs a system prompt describing your own route, supplied via `--llm-system-prompt` or `--llm-system-prompt-file`: which stations you actually use, in what order, and which kinds of alteration (e.g. ones confined to the other end of the line, or a single late-night working) don't matter to you.
There's no built-in default — that route-shape knowledge isn't something `--filter`'s bare list of terms carries, and baking one specific route into a tool meant to cover the whole network would defeat the point.

**Tip:** say which direction your station list runs (e.g. "in order from north to south: ...").
Left to infer it, even a strong model can get tripped up on geography that was never actually stated — observed on a real notice against an earlier, weaker local model, where it spent its entire reasoning budget contradicting itself over which of two stations was further along the line instead of answering the question.

#### Why `claude -p` and not a local model

This was originally built around a local CPU model (Qwen3.5-4B-Instruct, via `llama-cpp-python`, thinking mode) specifically to avoid any external dependency or per-call cost.
On synthetic test notices it worked; on real ones, it essentially never reached a confident verdict — every genuinely ambiguous real notice ran out of its token budget without settling, and the tool's fail-open design silently papered over that by keeping the item anyway, which looked like it was working (plausible YES/NO calls) but wasn't actually resolving anything.
A side-by-side test against `claude -p --model sonnet --effort high` and `--model opus --effort high`, asked to reason before answering, resolved essentially all of the same real notices cleanly — including correctly rejecting several the local model had only kept by exhausting its retries — so the tool switched over.
The trade-off is real: this is no longer free, local, and offline — each newly-seen notice costs one small `claude` API call — but the [cache](#the-cache) means that cost is paid once per notice, not once per night.

Each surviving item is asked for a two-sentence reason alongside its verdict, which becomes that event's "Why this matters" line in the `.ics` description — useful when you're looking at the calendar later and want the "why", not just the "what".

A notice can fail to yield a settled verdict — the `claude` process errors, times out, or its output has no parseable `ANSWER:` line.
Each of those is retried, up to `LLM_MAX_ATTEMPTS` (2); this is a much smaller safety margin than the local model needed, since `claude -p` reliably resolves real notices rather than routinely exhausting its attempts — a notice reaching this fallback should be rare, not the common case.
If it still won't resolve, the notice is treated as genuinely too hard to rule out — not confidently irrelevant — and kept, with a reason noting that explicitly.
If this starts firing on every item, check whether this machine's `claude` login has expired — see "Known gap: login expiry" in its own `claude-rc.md` docs — since a fail-open design degrades silently rather than loudly.

### The cache

An item's notice text is stable day to day — its `INCxxx` id reappears verbatim on every day of its own date range, which is also what the de-duplication in "How it works" above relies on — so once a notice has been verified, re-verifying it on every subsequent nightly run is close to pure waste (and, unlike the old local model, now has a real cost attached).
Verdicts (and their reasons) are cached in `./llm-verify-cache.local.json` (or wherever `--llm-cache` points), keyed by the item id plus a hash of the notice text *and* the system prompt, so an edited notice or a run under a different `--llm-system-prompt` each get a fresh verdict rather than a stale or mismatched one.
Entries older than 120 days are dropped on write, since nothing in a default 60-day run should ever need one that old.
A model error or unparseable response is never cached — those keep the item (fail open) without recording a verdict, so the next run gets a real shot at it rather than repeating a guess forever.
Loading the cache also silently drops any entry from an older schema (e.g. one saved before the "reason" field existed), so a cache from before this switch just re-verifies fresh rather than crashing on a missing field.
