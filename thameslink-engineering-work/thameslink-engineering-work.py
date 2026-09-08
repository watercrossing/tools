#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""Turn Thameslink's day-by-day planned engineering work page into a single .ics calendar.

Usage:
    uv run thameslink-engineering-work.py [-o out.ics] [--days 60] [--start-date 2026-09-06] [--filter TEXT ...]
    uv run thameslink-engineering-work.py --filter "St Albans" --filter "St Pancras" \
        --llm-verify --llm-system-prompt-file my-route.txt

https://www.thameslinkrailway.com/service-updates/planned-engineering-work only shows one day at a
time (?date=YYYY-MM-DD) with no overview, so seeing what's coming up means clicking through every
day by hand. This fetches N consecutive days, pulls out each engineering-work item (the page already
renders them server-side, keyed by a stable "INCxxxxxxxx" id in the title element), de-duplicates the
ones that span multiple days, and writes one all-day VEVENT per item — ready to import into Google
Calendar or any other calendar app.

The site 403s a plain requests/curl User-Agent, so this sends a browser-like one; it still respects
robots.txt (nothing here is disallowed) and pauses --delay seconds between requests.

## --filter's blind spot, and what --llm-verify does about it

--filter is a plain substring match over an item's title, routes and description, so it can't tell "this
station is where the disruption is" from "this station is just the normal, unaffected origin of a service
whose alteration happens somewhere else further down the line". A late-night amendment that starts at one
of your stations and simply terminates early further down the route matches on that station name, even
though your actual journey is untouched.

--llm-verify re-checks each item that has *already passed* --filter by shelling out to `claude -p` (the
Claude Code CLI, already installed and logged in on this machine -- no separate API key needed) and drops
the ones it judges as that kind of incidental mention -- it never sees, and never costs anything for, an
item --filter has already discarded. This was tried first against a local CPU model (Qwen3.5-4B, thinking
mode); on real notices it essentially never reached a confident verdict, defaulting to "keep" on every
genuinely hard case rather than actually resolving it. Claude at --llm-effort high, asked to reason before
answering, resolves real notices cleanly and gets the geography and rule application right where the local
model didn't -- see this tool's README for the comparison this replacement is based on.

It needs a system prompt describing your own route and what counts as in-scope, supplied via
--llm-system-prompt or --llm-system-prompt-file: which stations you actually use, in what order, and which
kinds of alteration (e.g. ones confined to the other end of the line, or a single late-night working) don't
matter to you. There's no built-in default -- that route-shape knowledge isn't something --filter's bare
list of terms carries, and baking one specific route in here would make this generic tool only useful for
that route. Tip: say which direction your station list runs (e.g. "in order from north to south: ...");
even a strong model can get tripped up inferring geography that was never stated.

Each surviving item is asked for a two-sentence reason alongside its verdict, which becomes that event's
"Why this matters" line in the .ics -- worth having open when eyeballing the calendar, since it's not
always obvious in the moment why a given notice made the cut.

A notice can fail to yield a settled verdict (the claude process errors, times out, or its output has no
parseable ANSWER: line); each of those is retried up to LLM_MAX_ATTEMPTS times, and if it still won't
resolve, the notice is treated as genuinely too hard to rule out -- not confidently irrelevant -- and kept.
This should be rare in practice; unlike the local model, --llm-verify-debug essentially never shows one of
these once a session's `claude` login is valid (see "Known gap: login expiry" in this machine's
claude-rc.md docs if this tool is failing open on every item -- an expired login degrades silently, since
fail-open is deliberately quiet).

An item's notice text is stable day to day (its INCxxx id reappears verbatim on every day of its own date
range, which is also what collect()'s de-duplication relies on), so once verified it stays verified: results
are cached in ./llm-verify-cache.local.json (keyed by item id + a hash of the notice and system prompt, so an
edited notice or a different --llm-system-prompt each get a fresh verdict rather than a stale one), and a
run against the same notices under the same prompt costs nothing beyond the initial one. --no-llm-cache
turns this off; --llm-cache PATH points it elsewhere. Caching also means each *new* notice is one real
`claude` call -- small, but real -- so keep --filter tight rather than pointing --llm-verify at everything.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://www.thameslinkrailway.com/service-updates/planned-engineering-work"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

LLM_MODEL = "sonnet"
LLM_EFFORT = "high"
LLM_TIMEOUT_S = 120

# claude -p resolves real notices reliably (unlike the local model this replaced, which routinely exhausted its
# retries on real data -- see the README), so this is a small safety margin for transient failures (a network
# blip, a busy API), not a "genuinely hard notice" fallback. Exhausting this should be rare; it's still fail-open
# (kept), since a dropped real closure is worse than one spurious calendar entry.
LLM_MAX_ATTEMPTS = 2

# Notices are stable day to day (the same INCxxx id reappears verbatim on every day of its own range, which is
# also what collect()'s de-duplication relies on), so re-verifying one every night is almost always wasted CPU.
LLM_CACHE_PATH = Path(__file__).resolve().parent / "llm-verify-cache.local.json"
LLM_CACHE_MAX_AGE_DAYS = 120  # comfortably past --days' own default (60), so a still-relevant entry never expires

MONTHS = {name: i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1)}

# "26 September - 27 September 2026 11:59 PM" -- the year is only written once, against the end date.
DATE_RANGE_RE = re.compile(r"(?P<d1>\d{1,2})\s+(?P<m1>[A-Za-z]+)\s*-\s*(?P<d2>\d{1,2})\s+(?P<m2>[A-Za-z]+)\s+(?P<year>\d{4})")


def fetch_page(date_str):
    req = urllib.request.Request(f"{BASE_URL}?date={date_str}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_date_range(match):
    d1, m1, d2, m2, year = int(match["d1"]), MONTHS[match["m1"]], int(match["d2"]), MONTHS[match["m2"]], int(match["year"])
    end = date(year, m2, d2)
    start = date(year - 1 if m1 > m2 else year, m1, d1)  # a range crossing New Year's only names the later year
    return start, end


def extract_items(html, source_date):
    """Pull every real engineering-work entry out of one day's page.

    The accordion markup is reused for nav/footer sections too (id="Travel-information" etc), but
    only genuine engineering-work items get an id starting "INC", which is what we key on.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    for title in soup.select("span.c-accordion-item__title[id]"):
        item_id = title["id"]
        if not item_id.startswith("INC"):
            continue
        accordion_item = title.find_parent("div", class_="c-accordion-item")
        content = accordion_item.select_one(".c-accordion-item__content") if accordion_item else None
        if content is None:
            continue

        date_text = routes = None
        description_bits = []
        for p in content.find_all("p"):
            label = p.find("strong")
            label_text = label.get_text(strip=True) if label else ""
            body_text = p.get_text(" ", strip=True)
            if label_text.startswith("Date:"):
                date_text = body_text.removeprefix(label_text).strip()
            elif label_text.startswith("Routes affected:"):
                routes = body_text.removeprefix(label_text).strip()
            elif body_text:
                description_bits.append(body_text)

        match = DATE_RANGE_RE.search(date_text or "")
        if not match:
            print(f"warning: {item_id} on {source_date} has an unparsable Date field ({date_text!r}), skipping", file=sys.stderr)
            continue
        try:
            start, end = parse_date_range(match)
        except KeyError as e:
            print(f"warning: {item_id} on {source_date} has an unrecognised month ({e}), skipping", file=sys.stderr)
            continue

        items[item_id] = {
            "id": item_id,
            "title": title.get_text(strip=True),
            "start": start,
            "end": end,
            "routes": routes,
            "description": "\n\n".join(description_bits),
            "first_seen": source_date,
        }
    return items


def collect(start, days, delay, verbose):
    items = {}
    for i in range(days):
        date_str = (start + timedelta(days=i)).isoformat()
        try:
            html = fetch_page(date_str)
        except urllib.error.URLError as e:
            print(f"warning: failed to fetch {date_str}: {e}", file=sys.stderr)
            continue
        day_items = extract_items(html, date_str)
        for item_id, item in day_items.items():
            items.setdefault(item_id, item)  # same item repeats on every day of its own range; keep the first copy
        if verbose:
            print(f"{date_str}: {len(day_items)} item(s) ({len(items)} unique so far)", file=sys.stderr)
        if i < days - 1:
            time.sleep(delay)
    return items


def matches_filters(item, filters):
    haystack = " ".join(filter(None, [item["title"], item["routes"], item["description"]])).lower()
    return any(f.lower() in haystack for f in filters)


ANSWER_RE = re.compile(r"ANSWER:\s*(YES|NO)", re.IGNORECASE)


def llm_confirms_relevant(item, system_prompt, model=LLM_MODEL, effort=LLM_EFFORT, debug=False):
    """Ask claude -p whether this item is a genuine service change on the caller's route (as described by
    system_prompt), as opposed to merely naming one of its stations as the normal, unaffected origin/destination
    of a service whose actual alteration happens further down the line -- the distinction matches_filters can't
    make, since it only sees whether the words appear anywhere at all.

    Returns (relevant: bool, reason: str) -- reason is the model's own explanation, meant to end up in the
    calendar event so a human glancing at it later doesn't have to re-derive why the item was kept. Always
    returns a definite, cacheable verdict: a claude invocation that errors, times out, or produces no
    parseable ANSWER: line is retried up to LLM_MAX_ATTEMPTS times, and if it still won't resolve, the notice
    is treated as genuinely too hard to rule out (kept, with a reason saying so) rather than confidently
    irrelevant. --restricted and --permission-prompts none keep this a pure text-in/text-out classification
    call: no tool use, nothing that could block waiting for a human who isn't there."""
    parts = [item["title"]]
    if item["routes"]:
        parts.append(f"Routes affected: {item['routes']}")
    if item["description"]:
        parts.append(item["description"])
    notice = "\n".join(parts)
    prompt = (
        f"Notice:\n{notice}\n\n"
        "Does this notice matter to the user? First, in two sentences, explain your reasoning, reasoning only "
        "from the text above. Then finish with a line reading exactly 'ANSWER: YES' or 'ANSWER: NO'."
    )
    cmd = [
        "claude", "-p", "--model", model, "--effort", effort,
        "--restricted", "--permission-prompts", "none",
        "--system-prompt", system_prompt,
        prompt,
    ]

    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        tag = f"{item['id']} (attempt {attempt}/{LLM_MAX_ATTEMPTS})"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LLM_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"warning: claude -p failed for {tag} ({e})", file=sys.stderr)
            continue
        text = proc.stdout.strip()
        if debug:
            print(f"\n----- claude -p transcript for {tag}: {item['title']!r} -----\n{text}\n"
                  f"{'(stderr: ' + proc.stderr.strip() + ')' if proc.returncode != 0 else ''}", file=sys.stderr)
        matches = list(ANSWER_RE.finditer(text))
        if not matches:
            print(f"warning: claude -p gave no parseable ANSWER: line for {tag} "
                  f"(exit {proc.returncode})", file=sys.stderr)
            continue
        last = matches[-1]
        reason = text[:last.start()].strip()
        return last[1].upper() == "YES", reason

    print(f"warning: claude -p never settled on {item['id']} after {LLM_MAX_ATTEMPTS} attempts, "
          f"treating as genuinely ambiguous and keeping it", file=sys.stderr)
    return True, f"Could not get a confident classification after {LLM_MAX_ATTEMPTS} attempts; kept as a precaution."


def cache_key(item, system_prompt):
    """Keyed on the item id plus a hash of the notice text and system_prompt, so a notice that's edited after
    first being seen -- or a caller running --llm-verify with a different route -- gets a fresh verdict rather
    than a stale or mismatched one."""
    h = hashlib.sha256(system_prompt.encode("utf-8"))
    for part in (item["title"], item["routes"] or "", item["description"]):
        h.update(part.encode("utf-8"))
    return f"{item['id']}:{h.hexdigest()[:16]}"


def load_llm_cache(path):
    if not path.exists():
        return {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: couldn't read --llm-cache at {path} ({e}), starting fresh", file=sys.stderr)
        return {}
    # drop anything from an older cache schema (e.g. pre-"reason") instead of crashing on a stale hit later
    return {k: v for k, v in cache.items() if "answer" in v and "reason" in v}


def save_llm_cache(path, cache):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LLM_CACHE_MAX_AGE_DAYS)).isoformat()
    pruned = {k: v for k, v in cache.items() if v["checked_at"] > cutoff}  # drop entries too old to still matter
    path.write_text(json.dumps(pruned, indent=2, sort_keys=True), encoding="utf-8")


def escape_text(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545 forbids content lines over 75 octets; continuations start with a space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks, start, limit = [], 0, 75
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:  # don't split a multi-byte UTF-8 sequence
            end -= 1
        chunks.append(encoded[start:end])
        start, limit = end, 74  # continuation lines lose one octet to their leading space
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def build_ics(items):
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//tools.ibecker.eu//thameslink-engineering-work//EN",
        "CALSCALE:GREGORIAN", "X-WR-CALNAME:Thameslink planned engineering work",
    ]
    for item in sorted(items.values(), key=lambda i: (i["start"], i["id"])):
        description_bits = []
        if item.get("llm_reason"):
            description_bits.append(f"Why this matters: {item['llm_reason']}")
        if item["routes"]:
            description_bits.append(f"Routes affected: {item['routes']}")
        if item["description"]:
            description_bits.append(item["description"])
        description_bits.append(f"Source: {BASE_URL}?date={item['first_seen']}")

        lines += [
            "BEGIN:VEVENT",
            fold(f"UID:{item['id']}@thameslinkrailway.com"),
            fold(f"DTSTAMP:{generated_at}"),
            fold(f"DTSTART;VALUE=DATE:{item['start'].strftime('%Y%m%d')}"),
            fold(f"DTEND;VALUE=DATE:{(item['end'] + timedelta(days=1)).strftime('%Y%m%d')}"),  # DTEND is exclusive
            fold(f"SUMMARY:{escape_text(item['title'])}"),
            fold(f"DESCRIPTION:{escape_text(chr(10).join(description_bits))}"),
        ]
        if item["routes"]:
            lines.append(fold(f"LOCATION:{escape_text(item['routes'])}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-date", type=date.fromisoformat, default=None,
                         help="First date to check, YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=60, help="Number of days to check (default: 60)")
    parser.add_argument("-o", "--output", default="thameslink-engineering-work.ics", help="Output .ics file")
    parser.add_argument("--filter", action="append", default=[], metavar="TEXT",
                         help="Only keep events whose title, routes or description contain TEXT (case-insensitive). "
                              "Repeatable; matches are OR'd. Default: keep everything")
    parser.add_argument("--llm-verify", action="store_true",
                         help="Double-check each --filter match by shelling out to claude -p and drop the ones "
                              "it judges as only naming a station as the unaffected origin/destination of a "
                              "service altered elsewhere. Only ever runs on items that already passed --filter "
                              "-- it costs nothing for the rest. Requires --filter and one of "
                              "--llm-system-prompt / --llm-system-prompt-file. Needs the claude CLI installed "
                              "and logged in; each new notice is one real (small) API call")
    parser.add_argument("--llm-system-prompt", metavar="TEXT",
                         help="With --llm-verify: the system prompt describing your route (which stations, in "
                              "what order, and what kinds of alteration don't matter to you). Mutually exclusive "
                              "with --llm-system-prompt-file")
    parser.add_argument("--llm-system-prompt-file", type=Path, metavar="PATH",
                         help="With --llm-verify: read the system prompt from this file instead of "
                              "--llm-system-prompt")
    parser.add_argument("--llm-model", default=LLM_MODEL, metavar="NAME",
                         help=f"With --llm-verify: model passed to claude -p --model (default: {LLM_MODEL})")
    parser.add_argument("--llm-effort", default=LLM_EFFORT, metavar="LEVEL",
                         help=f"With --llm-verify: effort level passed to claude -p --effort (default: {LLM_EFFORT})")
    parser.add_argument("--llm-verify-debug", action="store_true",
                         help="With --llm-verify, print each item's full claude -p response (reasoning included) "
                              "to stderr, for eyeballing the model's judgement instead of trusting it blind")
    parser.add_argument("--llm-cache", type=Path, default=LLM_CACHE_PATH, metavar="PATH",
                         help="With --llm-verify: cache verdicts here, keyed by item id plus a hash of the "
                              "notice and system prompt, so a notice already seen isn't re-verified every run "
                              f"(default: {LLM_CACHE_PATH})")
    parser.add_argument("--no-llm-cache", action="store_true",
                         help="With --llm-verify: don't read or write --llm-cache, always re-verify")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to sleep between requests (default: 0.5)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Don't print per-day progress")
    args = parser.parse_args()

    if args.llm_verify and not args.filter:
        parser.error("--llm-verify requires --filter (there's nothing to verify relevance against)")
    if args.llm_system_prompt and args.llm_system_prompt_file:
        parser.error("--llm-system-prompt and --llm-system-prompt-file are mutually exclusive")
    if args.llm_verify and not (args.llm_system_prompt or args.llm_system_prompt_file):
        parser.error("--llm-verify requires --llm-system-prompt or --llm-system-prompt-file (no built-in default "
                      "-- see the module docstring)")

    start = args.start_date or date.today()
    items = collect(start, args.days, args.delay, verbose=not args.quiet)
    if args.filter:
        before = len(items)
        items = {k: v for k, v in items.items() if matches_filters(v, args.filter)}
        print(f"kept {len(items)} of {before} item(s) after filtering", file=sys.stderr)

        if args.llm_verify:
            system_prompt = args.llm_system_prompt or args.llm_system_prompt_file.read_text(encoding="utf-8")
            cache = {} if args.no_llm_cache else load_llm_cache(args.llm_cache)
            cache_hits = 0
            kept = {}
            for item_id, item in items.items():
                key = cache_key(item, system_prompt)
                if key in cache:
                    cache_hits += 1
                    relevant, reason = cache[key]["answer"] == "YES", cache[key]["reason"]
                    if args.llm_verify_debug:
                        print(f"----- {item['id']}: {item['title']!r} -- cached: {cache[key]['answer']} "
                              f"({reason}) -----", file=sys.stderr)
                else:
                    relevant, reason = llm_confirms_relevant(item, system_prompt, model=args.llm_model,
                                                              effort=args.llm_effort, debug=args.llm_verify_debug)
                    cache[key] = {"answer": "YES" if relevant else "NO", "reason": reason,
                                  "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                if relevant:
                    kept[item_id] = {**item, "llm_reason": reason}
            items = kept
            if not args.no_llm_cache:
                save_llm_cache(args.llm_cache, cache)
            print(f"kept {len(items)} of {before} item(s) after llm verification "
                  f"({cache_hits} from cache)", file=sys.stderr)

    Path(args.output).write_text(build_ics(items), encoding="utf-8")
    print(f"wrote {len(items)} event(s) to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
