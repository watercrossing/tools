#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "beautifulsoup4",
# ]
# ///
"""Convert a Microsoft Teams meeting-chat HTML export into clean Markdown.

Usage:
    uv run teams-chat-to-markdown.py <input.html> [output.md]

If output.md is omitted, the Markdown is written next to the input file with a
.md extension. The input is the copied HTML subtree of the Teams message pane
(the element with id="chat-pane-list" / data-tid="message-pane-list-runway").
See capturing-the-export.md for how to capture it.

The script anchors on stable `data-tid` / `id` attributes rather than Teams'
obfuscated CSS classes, so it survives cosmetic restyles. If Teams changes the
underlying structure, see the "How the DOM maps to output" section of README.md.
"""
import sys
import re
import datetime
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

UTC = datetime.timezone.utc
QMARK = "\x00Q\x00"  # marks blockquote lines we emit, so prose-escaping leaves them alone

# Globals set in main() and read by the helpers below.
SOUP = None
OFFSET = datetime.timedelta(0)
MEETING_DAY = ""


def derive_offset(soup):
    """Client local-time offset, from a message's UTC `datetime` vs its visible HH:MM."""
    for tm in soup.find_all("time"):
        dt_attr = tm.get("datetime")
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", tm.get_text(strip=True))
        if not dt_attr or not m:
            continue
        try:
            utc = datetime.datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta = (int(m.group(1)) * 60 + int(m.group(2))) - (utc.hour * 60 + utc.minute)
        while delta > 720:
            delta -= 1440
        while delta < -720:
            delta += 1440
        return datetime.timedelta(minutes=delta)
    return datetime.timedelta(0)


def local_dt(ts_ms):
    """Epoch-ms (from a message id) -> client-local datetime."""
    return datetime.datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC) + OFFSET


def hhmm(ts_ms):
    return local_dt(ts_ms).strftime("%H:%M")


def link_md(a):
    txt = a.get_text(strip=True)
    href = a.get("href", "")
    if not txt:
        txt = href
    if href and href != txt:
        return f"[{txt}]({href})"
    return txt


def render_quote(card):
    """Render a Teams reply-quote card as a Markdown blockquote line."""
    ts_el = card.find(attrs={"data-tid": "quoted-reply-timestamp"})
    pv_el = card.find(attrs={"data-tid": "quoted-reply-preview-content"})
    # Author is the first StyledText span in the card that isn't the timestamp/preview.
    author = ""
    for sp in card.find_all("span"):
        if "fui-StyledText" not in (sp.get("class") or []):
            continue
        if sp is ts_el or sp is pv_el:
            continue
        t = sp.get_text(strip=True)
        if t:
            author = t
            break
    # Timestamp "09/07/2026 09:49" -> "09:49" (keep DD/MM if not the meeting day).
    tm = ts_el.get_text(strip=True) if ts_el else ""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}:\d{2})", tm)
    if m:
        d, mo, y, hm = m.groups()
        tm = hm if f"{y}-{mo}-{d}" == MEETING_DAY else f"{d}/{mo} {hm}"
    preview = re.sub(r"\s+", " ", serialize(pv_el)).strip() if pv_el else ""
    head = f"**{author}** · {tm}" if tm else f"**{author}**"
    return f"\n\n{QMARK}> {head}: {preview}\n\n"


def serialize(node):
    """Convert a content node to inline Markdown text."""
    out = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
        elif isinstance(child, Tag):
            if child.get("data-tid") == "quoted-reply-card":
                out.append(render_quote(child))
                continue
            name = child.name.lower()
            if name == "img":
                out.append(child.get("alt", "") or "")  # emoji live in the alt text
            elif name == "a":
                out.append(link_md(child))
            elif name == "br":
                out.append("\n")
            elif name == "p":
                out.append("\n\n" + serialize(child).strip() + "\n\n")
            else:
                out.append(serialize(child))
    return "".join(out)


def clean_block(text):
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    # Escape a leading markdown heading/quote marker so prose isn't misread,
    # but keep the blockquote lines we generated ourselves (marked with QMARK).
    esc = []
    for ln in text.split("\n"):
        if ln.startswith(QMARK):
            esc.append(ln[len(QMARK):])
        elif re.match(r"^\s*#", ln) or re.match(r"^\s*>", ln):
            esc.append("\\" + ln.lstrip())
        else:
            esc.append(ln)
    return "\n".join(esc)


def find_body(el):
    """Nearest ancestor whose class marks it a message body (holds the reaction pills)."""
    p = el
    for _ in range(12):
        p = p.parent
        if p is None:
            return None
        if "__body" in " ".join(p.get("class") or []):
            return p
    return None


def reactions_for(body):
    out = []
    for pill in body.find_all(attrs={"data-tid": "diverse-reaction-pill-button"}):
        img = pill.find("img")
        emoji = img.get("alt") if img else None
        txt = pill.get_text(" ", strip=True)
        mnum = re.match(r"^\s*(\d+)", txt)
        count = mnum.group(1) if mnum else ""
        if not emoji:
            emoji = re.sub(r"\d.*$", "", txt).strip() or "reaction"
        out.append((emoji, count))
    return out


def main():
    global SOUP, OFFSET, MEETING_DAY
    if len(sys.argv) < 2:
        sys.exit("Usage: uv run teams-chat-to-markdown.py <input.html> [output.md]")
    src = Path(sys.argv[1])
    if not src.exists():
        sys.exit(f"Input file not found: {src}")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".md")

    SOUP = BeautifulSoup(src.read_text(encoding="utf-8", errors="replace"), "html.parser")
    OFFSET = derive_offset(SOUP)

    content_els = [
        el for el in SOUP.find_all(attrs={"data-message-content": ""})
        if re.match(r"content-\d+$", el.get("id", ""))
    ]
    if not content_els:
        sys.exit(
            "No chat messages found. The export looks empty or truncated.\n"
            "Reopen the chat in a browser, scroll to the very TOP and BOTTOM to load\n"
            "every message (Teams virtualizes the list), then re-copy the message-pane\n"
            "subtree. See capturing-the-export.md."
        )

    # Meeting day = the date most messages fall on.
    day_counts = Counter(
        local_dt(re.match(r"content-(\d+)", el["id"]).group(1)).strftime("%Y-%m-%d")
        for el in content_els
    )
    MEETING_DAY = day_counts.most_common(1)[0][0]

    # Collect messages + control notices, keyed by timestamp for chronological order.
    items = []
    for el in SOUP.find_all(attrs={"data-message-content": ""}):
        mid = el.get("id", "")
        m = re.match(r"content-(\d+)$", mid)
        if m:
            ts = m.group(1)
            au = SOUP.find(id=f"author-{ts}")
            author = au.get_text(strip=True) if au else "Unknown"
            body = find_body(el)
            reacts = reactions_for(body) if body else []
            items.append((int(ts), "msg", author, clean_block(serialize(el)), reacts))
            continue
        mc = re.match(r"content-control-message-(\d+)$", mid)
        if mc:
            items.append((int(mc.group(1)), "ctrl", None, el.get_text(" ", strip=True), []))
    items.sort(key=lambda x: x[0])

    # Meeting name, if a "named the meeting X" control notice exists.
    meeting_name = ""
    for _, kind, _, text, _ in items:
        if kind == "ctrl":
            mm = re.search(r"named the meeting\s+(.+)$", text)
            if mm:
                meeting_name = mm.group(1).strip().rstrip(".").strip()

    msg_count = sum(1 for i in items if i[1] == "msg")
    authors = sorted({i[2] for i in items if i[1] == "msg"})
    d = datetime.datetime.strptime(MEETING_DAY, "%Y-%m-%d")
    date_disp = f"{d.day} {d:%B %Y}"

    lines = [
        f"# {meeting_name or 'Teams Meeting Chat'}",
        "",
        f"**Date:** {date_disp}  ",
        f"**Source:** Microsoft Teams meeting chat (`{src.name}`)  ",
        f"**Messages:** {msg_count} · **Participants:** {len(authors)}",
        "",
        "---",
        "",
    ]
    for ts, kind, author, text, reacts in items:
        if kind == "ctrl":
            dt = local_dt(ts)
            prefix = dt.strftime("%d %b %H:%M") + " — " if dt.strftime("%Y-%m-%d") != MEETING_DAY else ""
            lines += [f"*{prefix}{text}*", ""]
            continue
        lines += [f"**{author}** · {hhmm(ts)}", "", text if text else "*(no text)*", ""]
        if reacts:
            rtxt = " · ".join(f"{e} {c}" if c else e for e, c in reacts)
            lines += [f"_Reactions: {rtxt}_", ""]

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(f"Wrote {out_path}")
    print(f"  messages: {msg_count} | participants: {len(authors)} | meeting day: {MEETING_DAY}")
    if msg_count < 5:
        print("  WARNING: very few messages found — the capture may be truncated "
              "(did you scroll to both ends of the chat?).", file=sys.stderr)


if __name__ == "__main__":
    main()
