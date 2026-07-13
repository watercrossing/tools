# tools

A collection of small, self-contained tools, inspired by [Simon Willison's tools](https://tools.simonwillison.net/) ([source](https://github.com/simonw/tools/)).

Each tool lives in its own folder and stands alone.
See [CLAUDE.md](CLAUDE.md) for the conventions new tools follow.

## Microsoft Teams

- **[teams-chat-to-markdown](teams-chat-to-markdown/)** — convert a copied Teams meeting-chat HTML export into clean Markdown, preserving authors, timestamps, reply-quotes (as blockquotes), reactions, emoji, and links.
- **[teams-transcript-to-markdown](teams-transcript-to-markdown/)** — capture a Teams meeting transcript from the browser and convert it to Markdown, with consecutive speaker turns collapsed and a gap/completeness check for missing entries.
- **[teams-slidegrab](teams-slidegrab/)** — recover a deck that was shared live via PowerPoint Live but never handed over as a file: from a recording (OBS or Microsoft Stream), read the on-screen `Slide X of Y` counter with local OCR and save one clean screenshot per slide. CPU-only; nothing leaves the machine.
