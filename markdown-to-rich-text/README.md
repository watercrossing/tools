# markdown-to-rich-text

Turn Markdown into formatted text that pastes properly into Outlook and Word.

The copy button in a Claude chat (Claude Code, claude.ai) copies the reply as Markdown, so pasting it into an email gives you literal `## headings` and `**asterisks**`.
Selecting the rendered chat and copying that instead drops the heading levels and brings the chat's theme along.
This page puts an HTML version on the clipboard, which Outlook and Word turn into real formatting.

## Use

Open [`markdown-to-rich-text.html`](markdown-to-rich-text.html) in a browser, either from disk or from wherever the tools are hosted.

1. Copy the reply in Claude.
2. Click into the page and press **Ctrl+V**.
3. Paste into Outlook or Word.

With **Pasting replaces the text and copies the result straight away** ticked (the default), step 2 does the whole job: the paste replaces whatever was in the box, and the formatted version goes straight onto the clipboard.
Untick it to edit the Markdown first, with paste behaving normally, then press **Copy formatted text**.
The checkbox setting is remembered in the browser.

If headings arrive in Outlook as plain bold text, Outlook is pasting with **Merge formatting**, which keeps bold and lists but drops headings and paragraph spacing.
Pick **Keep source formatting** from the paste options after pasting, or make it the default under *File → Options → Mail → Editor Options → Advanced → Pasting from other programs*.

The clipboard gets two versions: HTML, which Outlook and Word use, and the original Markdown as plain text, for places that only take plain text.

## What the output looks like

- Headings, paragraphs and lists carry no styling of their own, so they take on the destination's styles: `#` becomes the **Heading 1** style, `##` **Heading 2**, and so on, in the document's or email's own font.
- The one exception is the size of body text (paragraphs, list items, table cells), set by **Text size** (11pt by default, remembered in the browser): left unsized, it pastes at the HTML default of 12pt rather than the destination's own size.
  Choose *destination's default* to leave it off.
- Everything else is styled inline, because Outlook and Word ignore stylesheets on paste: code is Consolas on a light grey background, table cells get borders, and block quotes get a grey bar on the left.
- Task-list checkboxes (`- [x]`) become ☑ / ☐, since form controls don't survive the paste.
- Relative links, such as Claude's links to files in a repo, keep their text but lose the link, and relative images are dropped, because neither would work in an email.
  `http(s)` and `mailto` links are kept.
- Raw HTML in the Markdown is sanitised with [DOMPurify](https://github.com/cure53/DOMPurify).

The preview shows exactly the HTML that gets copied.
Markdown is parsed with [marked](https://marked.js.org/) (GitHub-flavoured), loaded from jsdelivr.
A single line break is kept as a line break, as Claude and GitHub comments render it, instead of being joined into the paragraph as strict Markdown would.

## Tests

Playwright drives Chromium, copies, and reads back what's on the clipboard.
They need network access to reach the CDN.

```sh
uv run --with playwright playwright install chromium  # once, to get the browser
uv run tests/test_markdown_to_rich_text.py
```
