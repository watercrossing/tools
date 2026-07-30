# tools

A collection of small, self-contained tools, inspired by [Simon Willison's tools](https://tools.simonwillison.net/) ([source](https://github.com/simonw/tools/)).

Browse the rendered tools at **[tools.ibecker.eu](https://tools.ibecker.eu)**; the source lives on GitHub at **[watercrossing/tools](https://github.com/watercrossing/tools)**, where issues and pull requests are welcome.

Each tool lives in its own folder and stands alone.
See [CLAUDE.md](CLAUDE.md) for the conventions new tools follow.

## Microsoft Teams

- **[teams-chat-to-markdown](teams-chat-to-markdown/)** — convert a copied Teams meeting-chat HTML export into clean Markdown, preserving authors, timestamps, reply-quotes (as blockquotes), reactions, emoji, and links.
- **[teams-transcript-to-markdown](teams-transcript-to-markdown/)** — capture a Teams meeting transcript from the browser and convert it to Markdown, with consecutive speaker turns collapsed and a gap/completeness check for missing entries.
- **[teams-slidegrab](teams-slidegrab/)** — recover a deck that was shared live via PowerPoint Live but never handed over as a file: from a recording (OBS or Microsoft Stream), read the on-screen `Slide X of Y` counter with local OCR and save one clean screenshot per slide. CPU-only; nothing leaves the machine.

## Deployment

- **[github-push-deploy](github-push-deploy/)** — auto-deploy a GitHub repo on every push, using a repo webhook and a small PHP listener on a plain Apache + PHP-FPM box. The listener verifies the webhook's HMAC-SHA256 signature, then clones the repo and runs your own deploy script — publish files, run a build, restart a service, launch a container, whatever you put in it. No CI runner or third-party service; one `deploy.conf` drives it all.
- **[repo-web-view](repo-web-view/)** — publish a directory tree as a static, GitHub-style browsable site: every folder becomes an `index.html` showing its rendered `README.md` above a listing of the folder's contents, and a generated `.htaccess` makes Apache download files on click while folders render. Self-contained pages (inlined CSS, embedded README images). Pairs with **github-push-deploy** as the publish step.

## Analytics

- **[goatcounter-backfill](goatcounter-backfill/)** — replay archived Apache/nginx `combined` access logs into GoatCounter without collapsing visitor sessions. GoatCounter expires sessions after 8 hours of *wall-clock* inactivity, so `goatcounter import` replays an entire archive inside one session window: a visitor returning daily to the same page becomes a single visit stamped on day one, and the later days produce no rows at all. This tool assigns sessions from *log* time via the `session` field of `/api/v0/count`, and mirrors the `-exclude` rules so backfilled data matches what the live importer collects.

## Claude Code

- **[claude-render-transcripts](claude-render-transcripts/)** — render a Claude Code session `.jsonl` transcript (including headless `claude -p` runs that never appear in the `/resume` picker) into readable plain text: one header per turn and `text` / `thinking` / `tool_use` / `tool_result` blocks flattened, with long tool inputs and results truncated.

## Overleaf

- **[overleaf-comments-export](overleaf-comments-export/)** — a Tampermonkey userscript that syncs Overleaf review comments into the LaTeX source as `\olc` macros (author, timestamp, highlighted span, comment), each on its own line just above the line it annotates, so they land in git. Idempotent via a per-line `%olcsync` marker (insert new, update changed, never duplicate); resolved threads skipped by default; one macro per reply. Writing is a real edit that propagates to collaborators, so it confirms first.
