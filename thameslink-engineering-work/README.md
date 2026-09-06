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
