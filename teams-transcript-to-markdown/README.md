# teams-transcript-to-markdown

Capture a Microsoft Teams meeting transcript from the browser and convert it to clean Markdown, with consecutive speaker turns collapsed and a completeness check that flags missing entries and suspicious time gaps.

Teams renders the transcript as a **virtualized list**: only the rows near the viewport exist in the DOM at any moment, and off-screen rows are destroyed as you scroll.
You can't just select-all and copy.
This tool works in two steps — a browser-side grabber that scrolls the whole list and accumulates every row, and a converter that turns the saved HTML into Markdown.

## Step 1 — capture the transcript HTML

[grab-transcript.js](grab-transcript.js) runs in the browser DevTools console.
It watches the virtualized list and stores every `.ms-List-cell` (keyed by `data-list-index`, so re-rendered rows are de-duped) even as Teams removes rows that scroll out of view.

1. Open the meeting transcript so the transcript panel is visible.
2. Open DevTools (<kbd>F12</kbd>) → **Console**.
3. Paste the entire contents of `grab-transcript.js` and press <kbd>Enter</kbd>.
4. Run `await TG.auto()` — it scrolls top to bottom, capturing everything.
5. Run `TG.status()` — reports `captured / total` and lists any missing indices.
6. Run `TG.download()` to save `transcript.html` (or `TG.copy()` for the clipboard).

If step 3 logs `no transcript cells found`, the panel is inside an iframe: pick the correct frame in the console's context dropdown (top-left) and paste again.

## Step 2 — convert to Markdown

[teams-transcript-to-markdown.py](teams-transcript-to-markdown.py) is a single-file [uv](https://docs.astral.sh/uv/) script with no third-party dependencies:

```sh
uv run teams-transcript-to-markdown.py transcript.html transcript.md
```

Both arguments are optional and default to `transcript.html` and `transcript.md`.

The converter:

- preserves speaker names and the timestamp each turn started at;
- collapses consecutive segments from the same speaker into one block, carrying the name forward across continuation rows that have no header;
- renders system events (joins, screen-sharing, …) as blockquotes;
- appends a **gap / completeness check** that reports entries missing from the DOM (the definitive check for a virtualized list) and timestamp jumps over one minute, noting whether a jump spans missing rows or is just one long turn.

It also prints a summary to the terminal so you can confirm the whole transcript loaded before trusting the output.
