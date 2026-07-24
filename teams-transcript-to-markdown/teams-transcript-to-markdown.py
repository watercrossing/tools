#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Convert a Teams transcript HTML dump (.ms-List-surface innerHTML) to Markdown.

- Preserves speaker names and the timestamp at which each speaker's turn started.
- Collapses consecutive segments from the same speaker into one block.
- Detects gaps (>1 min) and missing entries, to confirm the whole transcript loaded.

Capture the source HTML with the companion grab-transcript.js first (see README).

Usage: uv run teams-transcript-to-markdown.py transcript.html transcript.md
"""
import html as htmllib
import re
import sys

GAP_THRESHOLD_SECONDS = 60


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_seconds(sr_text: str):
    """'2 hours 57 minutes 5 seconds' / '1 minute 59 seconds' / '3 seconds' -> int seconds."""
    if not sr_text:
        return None
    h = re.search(r"(\d+)\s*hour", sr_text)
    m = re.search(r"(\d+)\s*minute", sr_text)
    s = re.search(r"(\d+)\s*second", sr_text)
    if not (h or m or s):
        return None
    return (int(h.group(1)) if h else 0) * 3600 + \
           (int(m.group(1)) if m else 0) * 60 + \
           (int(s.group(1)) if s else 0)


def fmt_ts(sec):
    if sec is None:
        return None
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_dur(sec):
    m, s = divmod(sec, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def parse(html_text):
    """Return list of entry dicts: {index, name, ts_sec, text, is_event}."""
    # Split on cell boundaries; each piece after the first is one list cell.
    parts = re.split(r'data-list-index="(\d+)"', html_text)
    entries = []
    for i in range(1, len(parts), 2):
        index = int(parts[i])
        cell = parts[i + 1]

        name_m = re.search(r'itemDisplayName-\d+">([^<]*)</span>', cell)
        name = htmllib.unescape(name_m.group(1)).strip() if name_m else None

        ts_m = re.search(
            r'baseTimestamp-\d+"><span[^>]*>([^<]*)</span>', cell)
        ts_sec = parse_seconds(ts_m.group(1)) if ts_m else None

        # Spoken text (entryText) or a system event (eventText).
        txt_m = re.search(
            r'(?:sub-entry-\d+)[^>]*class="(?:entryText|eventText)-\d+[^>]*>(.*?)</div>',
            cell)
        is_event = bool(txt_m and "eventText" in txt_m.group(0))
        text = strip_tags(txt_m.group(1)) if txt_m else ""

        entries.append({"index": index, "name": name, "ts_sec": ts_sec,
                        "text": text, "is_event": is_event})
    return entries


def build_blocks(entries):
    """Collapse consecutive same-speaker entries into blocks, carrying the
    speaker name forward across continuation entries that have no header."""
    blocks = []
    cur_speaker = None
    for e in entries:
        if e["is_event"]:
            blocks.append({"event": e["text"], "ts_sec": e["ts_sec"]})
            cur_speaker = None  # a system event breaks any run
            continue
        speaker = e["name"] or cur_speaker or "Unknown speaker"
        cur_speaker = speaker
        if blocks and blocks[-1].get("speaker") == speaker:
            blocks[-1]["text"].append(e["text"])
        else:
            blocks.append({"speaker": speaker,
                           "ts_sec": e["ts_sec"],
                           "text": [e["text"]]})
    return blocks


def find_gaps(entries, total_expected):
    gaps = []

    # 1) Missing entry indices (definitive completeness check for a virtualized list).
    present = sorted(e["index"] for e in entries)
    ts_by_index = {e["index"]: e["ts_sec"] for e in entries}
    for a, b in zip(present, present[1:]):
        if b - a > 1:
            gaps.append({
                "kind": "missing-entries",
                "detail": f"entries {a+1}–{b-1} absent from the DOM "
                          f"({b - a - 1} entries)",
                "before_ts": ts_by_index.get(a),
                "after_ts": ts_by_index.get(b),
            })
    if present and present[0] != 0:
        gaps.append({"kind": "missing-entries",
                     "detail": f"entries 0–{present[0]-1} missing before the start",
                     "before_ts": None, "after_ts": ts_by_index.get(present[0])})
    if present and total_expected and present[-1] != total_expected - 1:
        gaps.append({"kind": "missing-entries",
                     "detail": f"entries {present[-1]+1}–{total_expected-1} "
                               f"missing after the end",
                     "before_ts": ts_by_index.get(present[-1]), "after_ts": None})

    # 2) Timestamp jumps > threshold between consecutive timestamped entries.
    #    Only header entries carry a timestamp; the continuation entries between
    #    two headers legitimately have none, so a jump with every index present
    #    is just a long single-speaker turn, not missing content.
    present_set = set(present)
    timed = [e for e in entries if e["ts_sec"] is not None]
    for a, b in zip(timed, timed[1:]):
        delta = b["ts_sec"] - a["ts_sec"]
        if delta > GAP_THRESHOLD_SECONDS:
            missing_between = any(
                idx not in present_set for idx in range(a["index"] + 1, b["index"]))
            note = ("  <<< spans MISSING entries — likely lost content"
                    if missing_between
                    else "  (all entries present — probably one long turn)")
            gaps.append({
                "kind": "time-jump",
                "missing_between": missing_between,
                "detail": f"{fmt_dur(delta)} between {fmt_ts(a['ts_sec'])} "
                          f"(entry {a['index']}) and {fmt_ts(b['ts_sec'])} "
                          f"(entry {b['index']}){note}",
                "before_ts": a["ts_sec"], "after_ts": b["ts_sec"],
            })
    return gaps


def to_markdown(blocks, entries, total_expected):
    out = ["# Meeting transcript", ""]
    present = len(entries)
    out.append(f"*{present} of {total_expected} transcript entries present in the "
               f"source HTML.*")
    out.append("")

    gaps = find_gaps(entries, total_expected)
    for b in blocks:
        if "event" in b:
            ts = fmt_ts(b["ts_sec"])
            out.append(f"> _{b['event']}_" + (f" ({ts})" if ts else ""))
            out.append("")
            continue
        ts = fmt_ts(b["ts_sec"])
        header = f"**{b['speaker']}**" + (f" ({ts})" if ts else "")
        out.append(header)
        text = " ".join(t for t in b["text"] if t).strip()
        out.append(text)
        out.append("")

    # Gap report appended at the end.
    out.append("---")
    out.append("")
    out.append("## Gap / completeness check")
    out.append("")
    if not gaps:
        out.append("No gaps >1 min and no missing entries detected. "
                   "Transcript appears complete.")
    else:
        out.append(f"**{len(gaps)} issue(s) found** (threshold: "
                   f"{GAP_THRESHOLD_SECONDS}s):")
        out.append("")
        for g in gaps:
            tag = "MISSING ENTRIES" if g["kind"] == "missing-entries" else "TIME GAP"
            out.append(f"- **{tag}** — {g['detail']}")
    out.append("")
    return "\n".join(out), gaps


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "transcript.html"
    dst = sys.argv[2] if len(sys.argv) > 2 else "transcript.md"
    html_text = open(src, encoding="utf-8").read()
    total = re.search(r'aria-setsize="(\d+)"', html_text)
    total_expected = int(total.group(1)) if total else None

    entries = parse(html_text)
    blocks = build_blocks(entries)
    md, gaps = to_markdown(blocks, entries, total_expected)
    open(dst, "w", encoding="utf-8").write(md)

    print(f"Wrote {dst}")
    print(f"Entries present: {len(entries)} of {total_expected}")
    print(f"Gap/completeness issues: {len(gaps)}")
    for g in gaps:
        tag = "MISSING ENTRIES" if g["kind"] == "missing-entries" else "TIME GAP"
        print(f"  [{tag}] {g['detail']}")


if __name__ == "__main__":
    main()
