# teams-chat-to-markdown

Capture a Microsoft Teams chat or meeting chat from the Teams web app and download it as clean Markdown, with authors, times, reply-quotes (as blockquotes), reactions, emoji and links preserved.

Teams has no "export chat" button, and its message list is both **virtualized** (only the rows near the viewport exist in the DOM, and off-screen rows are destroyed as you scroll) and **lazily loaded** (older messages are fetched from the server only as you scroll up).
You can't just select-all and copy.
The userscript scrolls the chat back to its first message itself, keeps every message it has seen (keyed by its epoch-millisecond id, so re-rendered rows are de-duped), and converts the result to Markdown in the browser.

## Install

1. Install [Violentmonkey](https://violentmonkey.github.io/) or [Tampermonkey](https://www.tampermonkey.net/).
2. Open the [raw file on GitHub](https://raw.githubusercontent.com/watercrossing/tools/main/teams-chat-to-markdown/teams-chat-to-markdown.user.js) and let the manager offer to install it.

The script's `@updateURL` points at that same raw file, so the manager picks up new versions on its own (or on demand via its "check for updates").
It only installs one whose `@version` is higher than the installed one — **bump `@version` in every change you push**, or nobody gets it.

It runs on `https://teams.microsoft.com/*`, `https://teams.cloud.microsoft/*` and `https://teams.live.com/*`.

## Use

1. Open the chat in the Teams web app: a group or one-to-one chat, or a meeting's **Chat** tab.
2. A **Chat → Markdown** button appears in the top-right corner of the message list; it hides while no chat is open.
3. Click it. It jumps to the newest message, scrolls up until Teams has nothing older to load (the button counts the messages captured), then downloads `<chat name> <date of first message>.md` and scrolls back down.

On a long chat the scroll back takes a while, since Teams fetches older messages in pages.
If the list stops loading before it reaches the beginning, the button shows `⚠ start not reached` and the Markdown says so under its header.
The userscript manager's menu has the same action, plus **Download captured chat HTML** for the raw rows captured (on their own, the rows currently on screen).

Without a userscript manager, paste the whole file into the DevTools console instead: it adds the same button and exposes `TC.run()`, `TC.status()`, `TC.downloadMarkdown()` and `TC.downloadHTML()`.

## What the Markdown contains

```
# <Meeting name>

- **Date:** 9 November 2026
- **Messages:** 47
- **Participants:** …

## Monday 9 November 2026

*14:59 — Meeting started*                                  ← system notices in italics

**Author** · 15:44 · edited

Message text, with **formatting**, [links](https://…), lists and emoji 🙂 preserved.

> **Quoted Author** · 15:40: the quoted message (truncated…)

The reply to that quote.

_Reactions: ❤️ 4 · 👍 2_
```

- The title is the meeting's name, from its "X named the meeting Y" notice, or otherwise the chat's name from the tab title.
- A `## date` heading starts each day, so a chat that runs over weeks stays readable; times are in your browser's time zone, as Teams shows them.
- Quoted replies are previews: Teams only keeps a *truncated* copy of the quoted message (ending in `…`), but the full original is in the log as its own message.
- File attachments appear as links to the file; inline images, which need a Teams login to view, as `*(image)*`.

## Converting a saved HTML capture

The converter needs no browser, only a DOM, so HTML saved with **Download captured chat HTML** (or a full-page dump of Teams) can be turned into Markdown with Node and [linkedom](https://github.com/WebReflection/linkedom), since the userscript exports its converter when `require`d:

```sh
npm install linkedom
node -e 'const { parseHTML } = require("linkedom"), fs = require("fs");
  process.stdout.write(require("./teams-chat-to-markdown.user.js").toMarkdown(parseHTML(fs.readFileSync("chat.html", "utf8")).document).markdown)' > chat.md
```

Times come out in Node's time zone; set `TZ` (e.g. `TZ=Europe/London`) to match the Teams client.

## How the DOM maps to output (for fixing if Teams changes)

The script relies only on stable `data-tid` / `id` / `data-mid` attributes, never on Teams' generated CSS class names.
`<ts>` is the message's epoch-ms id, the join key across a message's parts.

| Output element        | Source anchor |
|-----------------------|---------------|
| Message list          | `data-tid="message-pane-list-runway"` (`id="chat-pane-list"`); each direct child is one row |
| Message id / time     | `data-mid="<ts>"` on the message body; the time is that epoch-ms in local time |
| Message body text     | `id="content-<ts>"` (`data-message-content`) |
| Author name           | `id="author-<ts>"`; a message without one inherits the previous author |
| Edited marker         | `id="edited-<ts>"` |
| Reply-quote           | `data-tid="quoted-reply-card"` → author span + `quoted-reply-timestamp` + `quoted-reply-preview-content` |
| Reactions             | `data-tid="diverse-reaction-pill-button"` → emoji is the `<img alt>`, count is the leading number in its text |
| Emoji (inline)        | `<img alt="😀">`, rendered as the alt character |
| @mention              | a `<div data-mention-type="person">` inside the paragraph, kept inline |
| System / control note | `id="content-control-message-<ts>"`; its leading "Yesterday 14:59" is dropped for the time from `<ts>` |
| Still loading         | `data-testid="vl-placeholders"` with a height — the scroll back waits longer while it shows |
