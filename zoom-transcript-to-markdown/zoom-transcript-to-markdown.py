#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Convert the transcript panel of a Zoom cloud-recording share page (saved as HTML) to Markdown.

- One block per speaker turn, with the recording offset at which the turn started.
- Chat messages shown alongside the recording, if the dump contains them, go in their own section.
- Reports the recording length covered and checks for missing rows (see README).

Usage: uv run zoom-transcript-to-markdown.py recording.html [more.html ...] [-o OUTDIR]
"""
import argparse
import html as htmllib
import re
import sys
from pathlib import Path

GAP_THRESHOLD_SECONDS = 60
OFFSET_RE = re.compile(r", ((?:(\d+) hours? )?(\d+) minutes? (\d+) seconds?), ")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def fmt_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def split_label(label: str):
    """'Name, 0 hours 9 minutes 49 seconds, text' -> (name, seconds, text). Names and text may contain commas."""
    label = htmllib.unescape(label)
    m = OFFSET_RE.search(label)
    if not m:
        return None, None, label
    h, mi, s = (int(g or 0) for g in m.groups()[1:])
    return label[:m.start()].strip(), h * 3600 + mi * 60 + s, label[m.end():].strip()


def parse_rows(html_text: str, item_class: str, id_re: str):
    """Each row is an element with class `item_class` whose aria-label carries speaker, offset and text."""
    starts = [m for m in re.finditer(rf'<(?:li|div)\b[^>]*class="{item_class}"[^>]*>', html_text)]
    rows = []
    for i, m in enumerate(starts):
        tag = m.group(0)
        body = html_text[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(html_text)]
        label = re.search(r'aria-label="([^"]*)"', tag)
        name, sec, label_text = split_label(label.group(1)) if label else (None, None, "")
        idm = re.search(id_re, tag)
        header = re.search(r'class="user-name-span"[^>]*>([^<]*)<', body) or re.search(r'class="name"[^>]*>([^<]*)<', body)
        text = re.search(r'class="(?:text|html-content)"[^>]*>(.*?)</div>', body, re.S)
        rows.append({"id": int(idm.group(1)) if idm and idm.group(1).isdigit() else None,
                     "name": name, "sec": sec,
                     "header": htmllib.unescape(header.group(1)).strip() if header else None,
                     "text": strip_tags(text.group(1)) if text else label_text})
    return rows


def check(rows):
    """Return a list of human-readable issues that suggest rows are missing or misparsed."""
    issues = []
    ids = [r["id"] for r in rows if r["id"] is not None]
    if len(ids) != len(rows):
        issues.append(f"{len(rows) - len(ids)} row(s) without a numeric id; completeness by id cannot be checked for them")
    if ids != sorted(ids):
        issues.append("row ids are not in ascending order")
    by_id = {r["id"]: r for r in rows}
    for a, b in zip(sorted(ids), sorted(ids)[1:]):
        if b - a > 1:
            span = f"id {a + 1}" if b - a == 2 else f"ids {a + 1}–{b - 1}"
            issues.append(f"MISSING ROWS: {span} absent ({b - a - 1} row(s)) between "
                          f"{fmt_ts(by_id[a]['sec'] or 0)} and {fmt_ts(by_id[b]['sec'] or 0)}")
    issues += [f"row {r['id']}: no offset in its aria-label" for r in rows if r["sec"] is None]
    issues += [f"row {r['id']} ({fmt_ts(r['sec'] or 0)}): empty text" for r in rows if not r["text"]]
    issues += [f"row {r['id']}: header says a different speaker than its aria-label" for r in rows if r["header"] and r["header"] != r["name"]]
    # Zoom only renders the speaker header when the speaker changes; a change without one means a header row went missing.
    prev = None
    for r in rows:
        if r["header"] is None and prev is not None and r["name"] != prev:
            issues.append(f"row {r['id']} ({fmt_ts(r['sec'] or 0)}): speaker changed without a header row")
        prev = r["name"]
    timed = [r for r in rows if r["sec"] is not None]
    for a, b in zip(timed, timed[1:]):
        if b["sec"] < a["sec"]:
            issues.append(f"offset goes backwards: {fmt_ts(a['sec'])} (row {a['id']}) → {fmt_ts(b['sec'])} (row {b['id']})")
        elif b["sec"] - a["sec"] > GAP_THRESHOLD_SECONDS:
            issues.append(f"time gap: {fmt_ts(b['sec'] - a['sec'])} of silence between {fmt_ts(a['sec'])} and {fmt_ts(b['sec'])} "
                          f"(rows {a['id']}, {b['id']}; all ids present, so most likely genuine silence or an untranscribed stretch)")
    return issues


def blocks(rows):
    out = []
    for r in rows:
        if out and out[-1]["name"] == r["name"]:
            out[-1]["text"].append(r["text"])
        else:
            out.append({"name": r["name"] or "Unknown speaker", "sec": r["sec"], "text": [r["text"]]})
    return out


def convert(src: Path, dst: Path):
    html_text = src.read_text(encoding="utf-8")
    rows = parse_rows(html_text, "transcript-list-item", r'id="transcript-list-item-(\d+)"')
    chat = parse_rows(html_text, "chat-list-item", r'id="(\d+)-')
    if not rows:
        raise ValueError("no transcript-list-item rows found; is this the Zoom transcript panel?")
    secs = [r["sec"] for r in rows if r["sec"] is not None]
    first, last = min(secs), max(secs)
    issues = check(rows)
    speakers = sorted({r["name"] for r in rows if r["name"]})

    md = [f"# {src.stem}", "",
          f"- Recording length: at least {fmt_ts(last)} (offset of the last transcript row; the recording itself is not in the dump)",
          f"- Transcript spans {fmt_ts(first)}–{fmt_ts(last)} ({fmt_ts(last - first)})",
          f"- {len(rows)} transcript rows, {len(chat)} chat messages",
          f"- Speakers: {', '.join(speakers)}", "", "## Transcript", ""]
    for b in blocks(rows):
        md += [f"**{b['name']}** ({fmt_ts(b['sec'] or 0)})", " ".join(t for t in b["text"] if t), ""]
    if chat:
        md += ["## Chat", ""]
        md += [f"- **{c['name']}** ({fmt_ts(c['sec'] or 0)}): {c['text']}" for c in chat]
        md.append("")
    md += ["## Completeness check", ""]
    md += [f"- {i}" for i in issues] or ["No missing rows, ordering problems or gaps over "
                                         f"{GAP_THRESHOLD_SECONDS}s detected."]
    dst.write_text("\n".join(md) + "\n", encoding="utf-8")
    return rows, chat, first, last, issues


def main():
    global GAP_THRESHOLD_SECONDS
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path, help="saved HTML of the Zoom transcript panel")
    ap.add_argument("-o", "--outdir", type=Path, help="write the .md files here (default: next to each input)")
    ap.add_argument("--gap", type=int, default=GAP_THRESHOLD_SECONDS, help="report silences longer than this many seconds (default: %(default)s)")
    args = ap.parse_args()
    GAP_THRESHOLD_SECONDS = args.gap
    failed = False
    for src in args.inputs:
        dst = (args.outdir or src.parent) / f"{src.stem}.md"
        try:
            rows, chat, first, last, issues = convert(src, dst)
        except ValueError as e:
            print(f"{src.name}: ERROR {e}", file=sys.stderr)
            failed = True
            continue
        missing = [i for i in issues if not i.startswith("time gap")]
        print(f"{src.name}: length ≥ {fmt_ts(last)} (transcript {fmt_ts(first)}–{fmt_ts(last)}), {len(rows)} rows, "
              f"{len(chat)} chat, {len(missing)} completeness issue(s), {len(issues) - len(missing)} gap(s) > {GAP_THRESHOLD_SECONDS}s -> {dst.name}")
        for i in issues:
            print(f"    {i}")
        failed |= bool(missing)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
