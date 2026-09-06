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

https://www.thameslinkrailway.com/service-updates/planned-engineering-work only shows one day at a
time (?date=YYYY-MM-DD) with no overview, so seeing what's coming up means clicking through every
day by hand. This fetches N consecutive days, pulls out each engineering-work item (the page already
renders them server-side, keyed by a stable "INCxxxxxxxx" id in the title element), de-duplicates the
ones that span multiple days, and writes one all-day VEVENT per item — ready to import into Google
Calendar or any other calendar app.

The site 403s a plain requests/curl User-Agent, so this sends a browser-like one; it still respects
robots.txt (nothing here is disallowed) and pauses --delay seconds between requests.
"""
import argparse
import re
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
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to sleep between requests (default: 0.5)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Don't print per-day progress")
    args = parser.parse_args()

    start = args.start_date or date.today()
    items = collect(start, args.days, args.delay, verbose=not args.quiet)
    if args.filter:
        before = len(items)
        items = {k: v for k, v in items.items() if matches_filters(v, args.filter)}
        print(f"kept {len(items)} of {before} item(s) after filtering", file=sys.stderr)

    Path(args.output).write_text(build_ics(items), encoding="utf-8")
    print(f"wrote {len(items)} event(s) to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
