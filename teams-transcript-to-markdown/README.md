# teams-transcript-to-markdown

Capture a Microsoft Teams meeting transcript from its Stream recording page and download it as clean Markdown, with consecutive speaker turns collapsed and a completeness check that flags missing entries and suspicious time gaps.

Teams renders the transcript as a **virtualized list**: only the rows near the viewport exist in the DOM at any moment, and off-screen rows are destroyed as you scroll.
You can't just select-all and copy.
The userscript scrolls the whole list itself, keeps every row it has seen (keyed by `data-list-index`, so re-rendered rows are de-duped), and converts the result to Markdown in the browser.

## Install

1. Install [Violentmonkey](https://violentmonkey.github.io/) or [Tampermonkey](https://www.tampermonkey.net/).
2. Open the [raw file on GitHub](https://raw.githubusercontent.com/watercrossing/tools/main/teams-transcript-to-markdown/teams-transcript-to-markdown.user.js) and let the manager offer to install it.

The script's `@updateURL` points at that same raw file, so the manager picks up new versions on its own (or on demand via its "check for updates").
It only installs one whose `@version` is higher than the installed one — **bump `@version` in every change you push**, or nobody gets it.
GitHub caches raw files for about five minutes, so a check straight after a push can still see the old version.

It runs on `https://*.sharepoint.com/*stream.aspx*` — the page a Teams recording opens in, e.g. `https://liveuclac-my.sharepoint.com/personal/…/_layouts/15/stream.aspx?id=…Meeting%20Recording.mp4`.

## Use

1. Open the recording and open its **Transcript** panel.
2. A **Transcript → Markdown** button appears bottom-right once transcript rows are on the page.
3. Click it. It scrolls the transcript top to bottom (the button shows `captured/total`), then downloads `<recording name>.md`.

The button reports `⚠ N rows missing` if the list never rendered some rows; the Markdown is saved anyway and its gap check says where.
The userscript manager's menu has the same action, plus **Download captured transcript HTML** for the raw capture.

Without a userscript manager, or on a page the `@match` doesn't cover, paste the whole file into the DevTools console instead: it adds the same button and exposes `TG.run()`, `TG.status()`, `TG.downloadMarkdown()` and `TG.downloadHTML()`.
If the transcript panel sits inside an iframe, pick that frame in the console's context dropdown (top-left) first.

## What the Markdown contains

- a header with the meeting's metadata: title and recording start (both parsed from the recording's file name, which Teams writes as `<title>-YYYYMMDD_HHMMSS-Meeting Recording.mp4`), duration (from the video player), who recorded it (from SharePoint's REST API, if you may read the file's details), a link back to the recording, and every speaker in the transcript;
- speaker names and the timestamp each turn started at;
- consecutive segments from the same speaker collapsed into one block, carrying the name forward across continuation rows that have no header;
- system events (joins, screen-sharing, …) as blockquotes;
- a **gap / completeness check** at the end that reports entries missing from the DOM (the definitive check for a virtualized list) and timestamp jumps over one minute, noting whether a jump spans missing rows or is just one long turn.

## Converting a saved HTML capture

The converter needs no browser, so HTML saved with **Download captured transcript HTML** can be turned into Markdown with Node, since the userscript exports its converter when `require`d:

```sh
node -e 'process.stdout.write(require("./teams-transcript-to-markdown.user.js").toMarkdown(require("fs").readFileSync("transcript.html", "utf8")).markdown)' > transcript.md
```

The header then has only the speakers, since the rest of the metadata comes from the live page.
