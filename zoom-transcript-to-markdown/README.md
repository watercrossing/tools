# zoom-transcript-to-markdown

Convert the transcript panel of a Zoom cloud-recording share page into clean Markdown: one block per speaker turn, any chat messages shown alongside the recording, how long the recording runs, and a check for missing transcript rows.

Zoom lets you play a shared recording but often not download its transcript.
The share page does render the whole transcript into the DOM (unlike Teams, the list is not virtualised), so saving that one element's HTML is enough.

## Step 1: capture the panel HTML

Open the recording's share page with the transcript panel visible, and save the panel's outer HTML to a file, e.g. with the [copy-page-html](../copy-page-html/) userscript or DevTools (*Copy → Copy outerHTML*).
The element to pick is the one holding both the *Audio Transcript* list and, if present, the *Chat Messages* list: on the 2025 share page that is `section > div > div:nth-of-type(3) > div:nth-of-type(2)`.
Anything that contains the `transcript-list-item` rows works; the rest of the page is ignored.

## Step 2: convert

[zoom-transcript-to-markdown.py](zoom-transcript-to-markdown.py) is a single-file [uv](https://docs.astral.sh/uv/) script with no third-party dependencies:

```sh
uv run zoom-transcript-to-markdown.py recording.html [more.html ...] [-o OUTDIR] [--gap SECONDS]
```

Each `X.html` becomes `X.md`, next to it or in `OUTDIR`.
A one-line summary per file goes to the terminal.

## What it reads

Every transcript row is an `<li class="transcript-list-item" id="transcript-list-item-N">` whose `aria-label` is `Speaker, 0 hours 9 minutes 49 seconds, text`.
Zoom only renders the visible speaker name and timestamp when the speaker changes, so the `aria-label` is the source for every row, and the visible header is used to cross-check it.
Chat messages are `chat-list-item` elements with the same `aria-label` shape.

## Length and completeness

The dump contains no recording duration, so the reported length is the offset of the last transcript row: a lower bound, usually within seconds of the end.
The span from first to last row is reported too; a late first row usually means the recording started before anyone spoke.

The completeness check flags, and the script exits 1 on:

- **missing rows**: a hole in the `transcript-list-item-N` ids, which number the rows consecutively (they start at an arbitrary offset, so rows lost before the first or after the last cannot be detected);
- a **speaker change without a header row**, which means the row carrying the header was lost;
- offsets that go backwards, rows without an offset or text, and a header that disagrees with its row's `aria-label`.

Silences longer than `--gap` seconds (default 60) are listed but do not fail the run: with all ids present they are genuine silence or speech Zoom did not transcribe.
