# teams-chat-to-markdown

Convert a Microsoft Teams meeting-chat HTML export into clean, readable Markdown — authors, timestamps, reply-quotes (as blockquotes), reactions, emoji, and links all preserved.

Teams has no "export chat" button, so you capture the message-pane HTML yourself from the browser's dev tools and feed it to this tool.
It anchors on stable `data-tid` / `id` attributes rather than Teams' obfuscated CSS classes, so it survives cosmetic restyles.

## Usage

```bash
uv run teams-chat-to-markdown.py <input.html> [output.md]
```

- `<input.html>` is the copied HTML subtree of the Teams message pane — see [capturing-the-export.md](capturing-the-export.md) for how to grab it.
- If `output.md` is omitted, the Markdown is written next to the input file with a `.md` extension.
- Needs [uv](https://docs.astral.sh/uv/); it fetches Python and `beautifulsoup4` automatically.
  On Windows, set `PYTHONIOENCODING=utf-8` if the console errors on emoji — the written file is always UTF-8.

The tool auto-detects the meeting date, the client timezone offset (from a message's UTC `datetime` vs its shown time), and the meeting name (used as the title).
It prints the message and participant counts so you can sanity-check completeness.

## Capturing the export

Because Teams renders the chat as a *virtualized list*, only messages you have scrolled past exist in the page's HTML.
**Scroll to the very top and the very bottom** of the conversation before copying, or the capture will be silently incomplete.
Full steps are in [capturing-the-export.md](capturing-the-export.md); in short:

1. Open the chat in a web browser (`teams.microsoft.com`).
2. Scroll to the very top **and** bottom so every message loads.
3. DevTools (F12) → **Elements**, `Ctrl+F` for `chat-pane-list` (or `message-pane-list-runway`).
4. Right-click that element → **Copy → Copy element**.
5. Paste into a file, e.g. `chatlog.html`, and save.

## Checking the output

- Message count looks plausible for the meeting length.
- The first message is genuinely the start of the conversation.
  If it begins mid-meeting, the top didn't load — scroll higher and re-capture.
- Spot-check a reply-quote: it should render as a `>` blockquote with the quoted author + time, followed by the reply.

## Troubleshooting

- **Very few messages / missing the beginning** → truncated capture; the list wasn't scrolled to both ends.
  Re-capture.
- **Run-together text like `Name 09/07/2026 09:49 quoted text reply text`** → the reply-quote markup changed; check the `quoted-reply-card` mapping below.
- **Raw HTML tags or `&amp;` in the output** → the parser fell back; check the content anchor below.

## How the DOM maps to output (for fixing if Teams changes)

Each of these is a stable `data-tid` / `id` the tool relies on.
`<ts>` is the message's epoch-ms id, the join key across an author/timestamp/content triple.

| Output element        | Source anchor |
|-----------------------|---------------|
| Message body text     | element with `data-message-content=""` and `id="content-<ts>"` |
| Author name           | `id="author-<ts>"` (`data-tid="message-author-name"`) |
| Timestamp             | `<time id="timestamp-<ts>" datetime="…Z">` — `datetime` is UTC, text is local |
| Reply-quote           | `data-tid="quoted-reply-card"` → author span + `quoted-reply-timestamp` + `quoted-reply-preview-content` (**preview is truncated with `…`** — Teams does not store the full quoted text) |
| Reactions             | `data-tid="diverse-reaction-pill-button"` → emoji is the `<img alt>`, count is the leading number in its text |
| Emoji (inline)        | `<img alt="😀">` — rendered as the alt character |
| System / control note | `id="content-control-message-<ts>"` (e.g. "Meeting started", "named the meeting X") |
| Virtualization        | `virtual-list-loader` / `vl-placeholders` — the reason you must scroll both ends |

## Output format

```
# <Meeting name>

**Date:** … · **Messages:** N · **Participants:** M

---

*07 Jul 08:23 — Chat has been turned on for this meeting.*   ← system notices in italics

**Author** · 09:27

Message text, with [links](https://…) and emoji 🙂 preserved.

> **Quoted Author** · 09:49: the quoted message (truncated…)

The reply to that quote.

_Reactions: ❤️ 4 · 👍 2_
```
