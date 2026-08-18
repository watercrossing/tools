# repo-web-view

Publish a directory tree (a repo, a folder of docs) as a **static, GitHub-style browsable site**.
For every folder it generates an `index.html` that shows the folder's **rendered `README.md`** at the top, and **below it a listing** of that folder's contents.
Folders open as more rendered pages, and the repo's self-contained **`.html` tools open and run in the browser**; **every other file downloads** rather than displaying.
With `--render-markdown`, every `.md` file in the tree gets a rendered page too, so clicking one reads it instead of downloading it.
Every page carries a **search box** over the whole site — the rendered pages by heading section, and every file in the tree by name.

It is built to sit behind a plain **Apache** — the same "I have an SSH login and `/var/www`" box that [github-push-deploy](../github-push-deploy/) targets — and pairs with that tool as the *publish* step: a push regenerates the browsable site.

## What it produces

- One `index.html` per folder: breadcrumb → rendered `README.md` (if the folder has one) → a table listing the folder's entries (directories first, then files with sizes).
- Fully **self-contained pages**: the CSS is inlined and any local images referenced by a README are embedded as `data:` URIs. Nothing the pages need is a separate file that could get caught by the download rule.
- With `--render-markdown`, one **`NAME.md.html`** beside every `NAME.md`: the same page layout with that file rendered where the README would be, and the folder's listing underneath. The listing links to it, and so do `.md` links inside rendered markdown.
- A single **`search-index.js`** at the root, which every page loads the first time someone searches. See [Search](#search).
- A single **`.htaccess`** at the root that (1) forces `Content-Disposition: attachment` on every file *except* `.html` — the generated index pages and the self-contained HTML tools, which render — and (2) disables server-side handlers (PHP-FPM, CGI, …) so a `.php`/`.cgi`/… in the tree downloads as source instead of executing. The whole download-vs-render policy lives in one place.

Markdown is rendered with CommonMark + GitHub niceties: tables, strikethrough, task lists, autolinks, fenced code with **syntax highlighting** (Pygments), and GitHub-compatible heading anchors so in-page `#links` work.

## Requirements

- **[uv](https://docs.astral.sh/uv/)** — the script declares its Python and dependencies inline (PEP 723), so `uv run` fetches them into a throwaway env; there is nothing to install.
- To get the download behaviour, **Apache with `mod_headers`** (the generated `.htaccess` needs it). Any static host will still serve the rendered pages; only the force-download depends on Apache.

## Usage

```bash
uv run repo-web-view.py SOURCE OUTPUT        # or ./repo-web-view.py SOURCE OUTPUT
```

`SOURCE` is copied into `OUTPUT` (dotfiles skipped) and an `index.html` is generated in every folder.
`OUTPUT` must be **outside** `SOURCE` (it may not be the same folder or nested inside it).

Preview locally with a built-in server that mirrors the production download headers, so you can check that files download and folders render before deploying:

```bash
uv run repo-web-view.py . ../tools-site --serve      # http://localhost:8000
```

Rebuild over an existing output (what a deploy does) with `--force`:

```bash
uv run repo-web-view.py /var/www/tools/repo /var/www/tools/html --force
```

## Rendering every markdown file

By default only `README.md` is rendered (into its folder's `index.html`); any other `.md` in the tree is a file like any other, so clicking it downloads it.
`--render-markdown` changes that:

```bash
uv run repo-web-view.py . ../tools-site --render-markdown
```

- Every `NAME.md` gets a page next to it called `NAME.md.html` — breadcrumb (ending in the file's name), the rendered markdown, then the same listing the folder's `index.html` shows, so you can carry on browsing from where you landed.
- The file listing links `NAME.md` to that page. The row still shows the real file name and its size; the generated pages are not listed as entries of their own.
- Links **inside** rendered markdown that point at a local `.md` file are rewritten to its page (`[docs](docs.md#usage)` → `docs.md.html#usage`), so a README's cross-references open rendered rather than downloading. Remote, absolute (`/…`) and dangling links are left exactly as written.
- The `.md` source files are still published and still download — `NAME.md.html` is an addition, not a replacement, so a "view source" link to `NAME.md` keeps working.

`README.md` gets a page of its own too, even though its content is already at the top of the folder's `index.html`; that is what makes a link to `../other-tool/README.md` render.

## Search

Every generated page has a search box at the right of the breadcrumb row.
Click it, or press `/` or `Ctrl`/`Cmd`+`K`, and a panel opens over the page; `↑`/`↓` walk the results, `Enter` opens one, `Esc` closes.

Results come in two groups:

- **Pages** — one entry per *heading section* of every page the site renders, so a result links to the heading it matched (`repo-web-view/#options`) rather than to the top of the page.
  That means every folder's `README.md`, and — with `--render-markdown` — every other `.md` file as well.
  Sections of the same page collapse into a single result — its best-matching section, with the others behind a *N more on this page* link that expands in place — so one long page cannot crowd out everything else.
- **Files** — the name and path of every file and folder in the tree, so `deploy.sh` is findable without knowing which tool it belongs to.
  Folders open, a `.md` file opens its rendered page when it has one, and anything else downloads, exactly as it would from a listing.

The bar above the results counts every match; the list itself shows the 20 best pages and the 20 best files.

Matching is plain substring matching, and every word of the query has to appear somewhere in the entry.
A word matched at the start of a word beats one matched in the middle of one, a hit in a heading beats a hit in body text, and a hit in a file's name beats one elsewhere in its path.

### The index

The whole site shares one **`search-index.js`**, written at the root beside the `.htaccess`.
A page pulls it in the first time someone actually searches, so a visitor who never opens the box never downloads it.
It is loaded as a script rather than fetched as JSON, which keeps it working when the built site is opened straight off disk — `fetch()` of a sibling file is blocked there, a `<script>` tag is not.
The force-download `.htaccess` does not get in the way either: `Content-Disposition` applies to what the browser *navigates* to, not to a subresource a page pulls in.

The index holds the full text of every section, so its size tracks the amount of markdown you publish — for this repository, about 115 KB, or 40 KB over the wire once compressed.
It is worth making sure Apache compresses it:

```apache
<IfModule mod_deflate.c>
    AddOutputFilterByType DEFLATE text/html text/css application/javascript
</IfModule>
```

`--no-search` leaves out the box, the panel and the index file.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--title NAME` | source folder name | Label for the root breadcrumb. |
| `--exclude GLOB` | — | Filename glob to skip; repeatable. Dotfiles (`.git`, `.gitignore`, …) are always skipped. |
| `--force` | off | Overwrite `OUTPUT` if it already exists (it is removed and rebuilt). |
| `--footer-note TEXT` | — | Text appended to every page's footer, e.g. the commit a deploy built. Escaped, so it is text and nothing else; empty means no note. |
| `--footer-note-url URL` | — | Turn `--footer-note` into a link to this URL (ignored without a note). |
| `--render-markdown` | off | Give every `.md` file a rendered page (`NAME.md.html`) and link to it from the listing, instead of downloading it. |
| `--no-search` | off | Leave out the search box and the `search-index.js` it loads. |
| `--no-htaccess` | off | Don't write the force-download `.htaccess` (e.g. you configure the rule in the vhost). |
| `--serve [PORT]` | — | After building, serve `OUTPUT` with production-like download headers (default port `8000`). Put it last on the command line. |

## Apache: making files download

The generated `.htaccess` only takes effect if the directory allows those overrides. In your `<VirtualHost>`, on the served directory:

```apache
<Directory "/var/www/tools/html">
    AllowOverride FileInfo Indexes
    Require all granted
</Directory>
```

If you'd rather not enable `.htaccess`, run with `--no-htaccess` and put the rules straight in that `<Directory>` block instead:

```apache
<Directory "/var/www/tools/html">
    Require all granted
    <IfModule mod_headers.c>
        <FilesMatch ".">
            Header set Content-Disposition "attachment"
        </FilesMatch>
        <FilesMatch "(?i)\.html?$">
            Header set Content-Disposition "inline"
        </FilesMatch>
    </IfModule>
    RemoveHandler .php .phtml .cgi .fcgi .pl .py .rb .lua .sh .shtml
    <FilesMatch "(?i)\.(php[0-9]?|phtml|phps|phar|cgi|fcgi|pl|py|rb|lua|sh|shtml)$">
        SetHandler default-handler
    </FilesMatch>
    DirectoryIndex index.html
</Directory>
```

## Using it as the github-push-deploy publish step

In your repo's [`github-push-deploy/deploy.sh`](../github-push-deploy/deploy.sh), replace the "Publish the site" copy block with a build into a fresh directory that is then swapped in with renames, so the live site is never served mid-rebuild:

```bash
# --- Publish the site (rendered, GitHub-style browsable view) ----------------
SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
COMMIT_URL=""
if [ -n "$SHORT_SHA" ] && [ -n "${REPO_FULL_NAME:-}" ]; then COMMIT_URL="https://github.com/$REPO_FULL_NAME/commit/$(git rev-parse HEAD)"; fi

rm -rf "$BASE_DIR/html-new" "$BASE_DIR/html-old"
uv run repo-web-view/repo-web-view.py . "$BASE_DIR/html-new" --footer-note "$SHORT_SHA" --footer-note-url "$COMMIT_URL"
if [ -d "$BASE_DIR/html" ]; then mv "$BASE_DIR/html" "$BASE_DIR/html-old"; fi
mv "$BASE_DIR/html-new" "$BASE_DIR/html"
rm -rf "$BASE_DIR/html-old"
```

Add `--render-markdown` to that call if you want every `.md` in the repo readable in the browser rather than downloadable.
The deploy user needs `uv` on its `PATH`. The single `repo-web-view` call produces the whole tree — copied files, the `index.html` pages, and the `.htaccess` — and the two renames swap it in near-atomically. (For a single build with no swap, run it straight at `"$BASE_DIR/html" --force`, which clears and rebuilds in place.)

Every page's footer then ends with the deployed commit, linked to it on GitHub, which is what makes "is the live site up to date?" answerable from the site itself:

```html
<footer>Generated by repo-web-view · <a href="https://github.com/owner/repo/commit/e7963a3…">e7963a3</a></footer>
```

`git rev-parse` works in `update.sh`'s shallow clone, and `REPO_FULL_NAME` comes from `deploy.conf`. Both values may come out empty — a build from a copy with no `.git`, or a config without `REPO_FULL_NAME` — which just leaves the footer unstamped rather than failing the deploy.

## Notes and limitations

- **`.html` files render; every other file downloads.** The tools here are self-contained single HTML files, so exposing them to *run* in the browser is the whole point. If you have an HTML file you'd rather force to download, drop an `.htaccess` in its folder setting `Content-Disposition "attachment"` for that name. Note that a `.html` tool which pulls in *sibling* assets (a separate `.js`/`.css`) would have those download — but the repo convention is that web tools are single self-contained files, so this doesn't arise here.
- **Nothing is executed server-side.** The `.htaccess` disables handlers (PHP-FPM, CGI, …) and serves scripts statically, so a `.php`/`.cgi`/… in the tree downloads as source instead of running. Caveat: if your PHP is wired with `SetHandler "proxy:…"` inside a `<FilesMatch>` in the vhost (some PHP-FPM setups), that can out-rank `.htaccess`; to be certain, also turn execution off for this `DocumentRoot` in the vhost.
- **`--render-markdown` matches `.md` only** (case-insensitively), not `.markdown`/`.mdown`/`.txt`. A source file that is *already* called `NAME.md.html` next to a `NAME.md` would be overwritten by the generated page — rename one of the two.
- **README images** are inlined only when they point at a **local file that exists**; remote (`http(s)`) and absolute (`/…`) image URLs are left untouched.
- **No per-file commit info.** GitHub shows each file's last commit message and date; this shows name, type and size. Adding the commit columns would mean a `git log` per path — deliberately left out to keep the tool VCS-agnostic and fast. For the same reason the tool never runs `git` itself: `--footer-note` takes whatever string the caller worked out, so the site can carry one build-wide version stamp without the generator knowing what a commit is.
- **Search covers what the site renders, not what it publishes.** The `.py`, `.sh` and `.html` files in the tree are findable by *name*, but their contents are not indexed — only markdown that has a page to link to is. Without `--render-markdown` that is READMEs alone; a plain `.md` is a download, and a search result has nowhere to send you.
- **A result shows the first 200 characters of the section it matched**, while the whole section is searched. A match further down is a real hit, but the snippet may not be showing the part that matched, so a result can appear with nothing highlighted in it.
- **Folder links need a server.** Results that point at a folder (and the folder links in every listing) end in `/`, which only resolves to `index.html` through a web server. Search itself works when the site is opened off disk; those particular links do not.
- **Syntax-highlight colours are tuned for light mode.** Pages otherwise adapt to the viewer's light/dark theme via `prefers-color-scheme`.
