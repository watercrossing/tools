# overleaf-comments-export

A Tampermonkey userscript that syncs Overleaf review comments into your LaTeX source as `\olc` macros, so they end up in git alongside the text they refer to.
Overleaf keeps review comments in its own database, not in the `.tex`; this pulls them into the document — one macro per comment/reply, anchored right after the highlighted span, with the author, timestamp, highlighted text and comment body — and does it **idempotently**, so re-running only inserts new comments and updates changed ones rather than duplicating.

## Why a userscript (and `@grant none`)

Comment **text** comes from a credentialed same-origin call (`GET /project/<id>/threads`) and comment **positions** live in the editor's in-memory CodeMirror 6 state (the `ranges` field: `op:{c,p,t}` = highlighted text, char offset, thread id).
Both are only reachable from **inside the page**, so the script runs with `@grant none` (page context, no sandbox) to touch the app's live editor objects and issue the credentialed fetch.
It never scrapes the DOM for content — CM6 only renders the visible slice, so the in-memory state is the single source of truth.

## Install

1. Install [Tampermonkey](https://www.tampermonkey.net/) (or a compatible userscript manager).
2. Create a new script and paste the contents of [`overleaf-comments-export.user.js`](overleaf-comments-export.user.js), or open the raw file and let Tampermonkey offer to install it.
3. Open a project on `https://www.overleaf.com/project/*` with the code editor visible.
4. A **Comment sync** dropdown appears in the editor's top navigation, next to **Help**.

## Use

The dropdown has two items:

- **Sync comments to tex on this file** — operates on the currently-open document. Fully supported.
- **Send comments to tex on all files (experimental)** — see [All files](#all-files-experimental).

Before writing anything, the script **logs every generated `\olc` line** and a summary (`inserts / replaces / skips / unanchored`) to the console, then asks for confirmation.

### Preamble: define `\olc`

Add a definition of the four-argument macro to your preamble so the document compiles.
The macro takes four **brace** arguments (`\olc{date}{author}{highlight}{comment}`), so it binds to a plain `\providecommand{\olc}[4]{…}` with no packages.
The default below needs no packages and always compiles — it renders each comment inline as a bold attribution:

```latex
% \olc{date}{author}{highlighted text}{comment}
\providecommand{\olc}[4]{\textbf{Comment by #2 on #1} on ``#3'': #4}
```

Prefer a margin note? A `todonotes` variant (add `\usepackage[textwidth=3cm]{todonotes}`):

```latex
\providecommand{\olc}[4]{\todo[color=yellow!40,size=\scriptsize]{\textbf{#2}, #1: #4}}
```

The macro is injected on its **own line(s), immediately before the line that contains the highlighted span**, so it sits next to the text it refers to without interrupting the sentence.
The highlighted text is passed as `#3` purely for reference; it is **truncated** to `first two [...] last two` words when it runs longer than five words, and may be empty for older comments (see below).

## How the sync stays idempotent

Each injected line carries a trailing marker:

```
\olc{13 May, 6:27 pm}{Ingolf Becker}{the highlighted span}{the comment text} %olcsync:<threadId>:<messageId>:<contentHash>
```

- `threadId` / `messageId` — the Overleaf thread and message this line represents (hex ids).
- `contentHash` — a short base36 [cyrb53](https://stackoverflow.com/a/52171480) hash over the raw `date + author + highlight + text` (with the highlight already collapsed and truncated).

On each run, for every message: **no marker** → insert; **marker present, hash matches** → skip; **marker present, hash differs** (someone edited the comment) → replace that whole line in place.
Comments are therefore never duplicated, and the hash covers the pre-escape values so cosmetic LaTeX-escaping changes don't churn it.
The marker sits after the macro on its **own line** — the `%` only comments out the marker itself, never any of your source.

## ⚠️ Writing edits the live document — and syncs to collaborators

**This is a real edit.**
The sync injects into the live CodeMirror document via a single CM6 transaction, exactly as if you typed it.
That means it **propagates to every collaborator on the project in real time**, and if **track-changes** is enabled it will be **captured as tracked changes**.
This is intentional — the goal is to get the comments into the `.tex` and then into git — but it is deliberate: the script **asks for confirmation first**, shows you the exact counts, and warns (best-effort) if it detects track-changes is on.
Track-changes detection is a DOM sniff and may be wrong; when in doubt, check the review panel yourself before confirming.

## Behaviour details

- **One `\olc` per message.** A thread's original comment and each reply become separate `\olc` lines, clustered on their own lines just **above** the annotated line, so replies keep their own author and timestamp.
- **Placement.** The macro is hoisted to the **start of the line** holding the highlighted span, so it never splits a sentence; the highlight is truncated to `first two [...] last two` words past five words.
- **Resolved threads are skipped by default.** (Toggle in code: call `window.olcSync.syncThisFile({includeResolved:true})` from the console.)
- **Empty highlight.** Older comments often have an empty highlighted span (`op.c === ""`); those anchor at the start of the line holding the comment's caret, with an empty `#3`.
- **Insertion order.** Multiple inserts into one file are planned end-first (offsets descending) so earlier insertions never invalidate later offsets; each lands on its own line above the text it annotates.
- **Unanchored threads.** Threads with no position in the open file (they belong to other files) are counted and skipped.

## All files (experimental)

There is **no project-level positions endpoint** — per-file `ranges` load only when a document is open.
"All files" therefore walks the `.tex` entries in the file tree, opens each in turn, waits for its ranges to load, syncs it (still confirming per file), then restores the originally-open document.
It is **DOM-driven and fragile**, gated behind an extra confirmation, and marked experimental in the code.
Get **this file** right first; treat all-files as best-effort.

## Depends on Overleaf internals

This reads Overleaf's in-memory CM6 state and an undocumented endpoint, and injects UI next to their nav.
**It may break when Overleaf updates.**
When an anchor or state field can't be found, the script fails **loudly to the console** (`[olc] …`) rather than silently doing the wrong thing — check the console first if a menu item seems to do nothing.

## Tests

Two layers: fast, hermetic unit tests for the pure core, and a live-site Playwright probe for the browser integration (menu, DOM anchors, CM6 state, `/threads`).

### Unit tests (core)

The pure core (escaping, timestamp/hash, marker parsing, annotation building, edit planning/idempotency) is unit-tested through V8 via [mini-racer](https://github.com/bpcreech/PyMiniRacer), driven by the userscript's own test-export hatch.
Run deterministically (the timestamp test pins `TZ=UTC`):

```bash
TZ=UTC uv run overleaf-comments-export/tests/test_core.py
```

### Live-site probe (Playwright)

[`tests/probe_overleaf.py`](tests/probe_overleaf.py) drives a **real Overleaf project** in a headless browser, injects the userscript in page context (exactly as `@grant none` does), and checks the things the pure core cannot: that the **Comment sync** menu injects next to **Help**, that its submenu opens on click and actually renders, and that the fragile selectors still match the live app — the CM6 `ranges` state field (found by shape, never a hardcoded index), the `/threads` payload, and the Help/file-tree anchors.
It is named `probe_*` (not `test_*`) so pytest never collects it: it hits the network and needs a browser plus a logged-in session.

One-time browser install (the binary is not a pip dependency, so it is fetched separately):

```bash
uv run --with playwright playwright install chromium
```

**Authentication — where to put the cookie.** Overleaf link-share edits ride on an account, so even an "anyone with the link" project needs a logged-in session; a fresh, cookie-less browser is bounced to `/login`.
Give the probe a session one of two ways, both read from a **gitignored `tests/.auth/` directory** and never printed or committed:

- **Session cookie (quickest).** In a logged-in Overleaf tab open DevTools → **Storage** → **Cookies** → `https://www.overleaf.com`, copy the value of **`overleaf_session2`**, and save just that value into `tests/.auth/overleaf_session.txt` (or export it as `$OVERLEAF_SESSION`).
  It is a live credential — treat it like a password, and re-copy it when it expires.
- **One-time headed login.** `uv run overleaf-comments-export/tests/probe_overleaf.py --login` opens a real window; sign in yourself (handles 2FA/SSO/reCAPTCHA) and open any project, and the session is saved to `tests/.auth/overleaf-state.json` for reuse.

Run it (defaults to headless Chromium against the shared test project):

```bash
uv run overleaf-comments-export/tests/probe_overleaf.py            # headless, cookie auth
uv run overleaf-comments-export/tests/probe_overleaf.py --headed   # watch it happen
uv run overleaf-comments-export/tests/probe_overleaf.py --browser firefox --url <project-or-share-url>
```

Screenshots (editor, menu, open submenu) land in the gitignored `tests/test-results/`.
The probe presents a normal desktop User-Agent and hides `navigator.webdriver`, because Overleaf bounces the default headless fingerprint to `/login`.

## Next steps

- **Capture real `/threads` + `ranges` payloads as fixtures.**
  Add a `--dump-fixtures` mode to the probe that writes the live `/threads` JSON and the CM6 `ranges.comments` array to `tests/fixtures/`, so the core unit tests can run against genuine Overleaf shapes instead of hand-written ones.
  Re-capturing and diffing against the committed fixtures then becomes the canary for **Overleaf changing its internals** — the moment a shape drifts (or a probe selector stops matching), the diff tells us exactly what to patch.
  Payloads carry author names, emails and comment text, so **sanitise before committing** (strip identities) or keep the raw captures gitignored and commit only a redacted subset.

- **Exercise the real sync (write) path repeatably via snapshot & restore.**
  The probe deliberately stops before the write; to test the actual `view.dispatch` end-to-end you must reset the document between runs, because the sync is idempotent (a second run is a no-op until the injected `%olcsync` markers are gone).
  The reliable, API-free way is a **CM6 snapshot/restore**: capture `view.state.doc.toString()` before syncing, run the sync, assert on the result, then restore with a single transaction replacing `[0 .. doc.length)` with the snapshot.
  This reverts the injected `\olc` lines exactly and needs no undocumented history endpoint; like the sync itself it is a real edit that propagates to collaborators and is caught by track-changes, so keep it on a throwaway project.
  The threads/comments are read-only inputs to the sync, so only the document text needs resetting between runs.
  Overleaf's native **History** ("Restore to this version") is a manual fallback, but it is a heavier, undocumented flow and not worth automating when the snapshot/restore already does the job.
