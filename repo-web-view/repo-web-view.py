#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "markdown-it-py[linkify]",
#     "mdit-py-plugins",
#     "pygments",
# ]
# ///
"""
repo-web-view.py — publish a directory tree as a static, GitHub-style browsable site.

For every folder it writes an index.html that shows the folder's rendered README.md
(if present) followed by a listing of the folder's contents. Rendered pages are fully
self-contained (CSS inlined, README images embedded as data URIs), so the only files
served as HTML are the generated index pages — everything else is meant to download.

With --render-markdown every .md file gets a page of its own too (NAME.md.html, same
layout: rendered markdown above the folder listing), and links to it — from the listing
and from inside other markdown — point at that page instead of downloading the source.

Every page carries a search box (top right, "/" or Ctrl-K) that searches the pages the site
renders — one result per heading section — and, in a second group, the name and path of every
file in the tree. The index is one shared search-index.js, loaded on first use; --no-search
leaves the whole thing out.

A generated .htaccess makes Apache force `Content-Disposition: attachment` on every
file except those index pages, so clicking any file downloads it while folders render.

Run (deps auto-installed from the inline metadata above):
    uv run repo-web-view.py SOURCE OUTPUT            # or ./repo-web-view.py SOURCE OUTPUT
    uv run repo-web-view.py . ../site --serve        # build, then preview at http://localhost:8000
    uv run repo-web-view.py . ../site --render-markdown   # every .md renders instead of downloading

SOURCE is copied into OUTPUT (dotfiles skipped) and an index.html is generated in every
folder. OUTPUT must not be the same as, or nested inside, SOURCE. Use --force to overwrite
a non-empty OUTPUT (what a deploy step wants).
"""
import argparse, base64, fnmatch, html, json, mimetypes, os, re, shutil, sys
from pathlib import Path
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight as pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

README_NAME = "readme.md"
INDEX_NAME = "index.html"
HTACCESS_NAME = ".htaccess"
MD_SUFFIX = ".md"
MD_PAGE_SUFFIX = ".md.html"   # page generated for FILE.md, next to it (an .html name, so it renders)
SEARCH_INDEX_NAME = "search-index.js"

# --------------------------------------------------------------------- markdown rendering
def _highlight(code, lang, _attrs):
    if not lang:
        return ""  # no language tag -> let markdown-it emit a plain, escaped code block
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""
    inner = pyg_highlight(code, lexer, HtmlFormatter(nowrap=True))
    return f'<pre class="code"><code class="hl language-{html.escape(lang)}">{inner}</code></pre>\n'

def _gh_slug(text):
    # Match GitHub's heading anchors: lowercase, drop punctuation, spaces -> hyphens.
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")

def make_markdown():
    md = MarkdownIt("commonmark", {"html": True, "linkify": True, "highlight": _highlight})
    md.enable(["table", "strikethrough", "linkify"], True)
    md.use(tasklists_plugin)
    md.use(anchors_plugin, max_level=6, slug_func=_gh_slug)
    return md

def _img_mime(path):
    return mimetypes.guess_type(path.name)[0] or {
        ".svg": "image/svg+xml", ".webp": "image/webp", ".avif": "image/avif",
        ".ico": "image/x-icon", ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "application/octet-stream")

def inline_images(html_str, base_dir):
    # Embed local README images as data URIs so pages stay self-contained and still
    # render even though the .htaccess forces every real file to download.
    def repl(m):
        pre, src, post = m.group(1), m.group(2), m.group(3)
        if re.match(r"^(?:https?:)?//", src) or src.startswith(("data:", "#", "/")):
            return m.group(0)
        path = base_dir / unquote(src.split("#")[0].split("?")[0])
        if not path.is_file():
            return m.group(0)
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"{pre}data:{_img_mime(path)};base64,{b64}{post}"
    return re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', repl, html_str, flags=re.I)

def link_md_pages(html_str, base_dir):
    # With --render-markdown a link to a local FILE.md should open the page generated for it, not
    # download the source; any query/fragment is kept, so cross-file #anchors still land.
    def repl(m):
        pre, href, post = m.group(1), m.group(2), m.group(3)
        if re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.I) or href.startswith(("//", "#", "/")):
            return m.group(0)
        path, tail = re.match(r"([^#?]*)(.*)", href).groups()
        if not path.lower().endswith(MD_SUFFIX) or not (base_dir / unquote(path)).is_file():
            return m.group(0)
        return f"{pre}{path}.html{tail}{post}"
    return re.sub(r'(<a\b[^>]*?\bhref=")([^"]*)(")', repl, html_str, flags=re.I)

def render_markdown(path, md, render_md):
    html_str = inline_images(md.render(path.read_text("utf-8", "replace")), path.parent)
    return link_md_pages(html_str, path.parent) if render_md else html_str

# --------------------------------------------------------------------- search index
TAG_RE = re.compile(r"<[^>]+>")
HEADING_RE = re.compile(r"<h([1-6])(?=[\s>])([^>]*)>(.*?)</h\1>", re.I | re.S)

def plain_text(html_str):
    return " ".join(html.unescape(TAG_RE.sub(" ", html_str)).split())

def index_sections(html_str, loc, title):
    # One record per heading, so a result can deep-link into the section it matched — the ids are the
    # ones the renderer already emits, the same ones a README's own #links use. Text before the first
    # heading becomes a record for the page itself. Sections are indexed whole; a result shows the
    # first SNIPPET characters of one, which is a display choice the page makes, not an index one.
    records, pos, section, anchor = [], 0, "", ""
    for m in HEADING_RE.finditer(html_str):
        body = plain_text(html_str[pos:m.start()])
        if body or section:
            records.append([loc + anchor, title, section, body])
        found = re.search(r'\bid="([^"]*)"', m.group(2))
        anchor, section, pos = ("#" + found.group(1) if found else ""), plain_text(m.group(3)), m.end()
    body = plain_text(html_str[pos:])
    if body or section:
        records.append([loc + anchor, title, section, body])
    return records

def file_records(entries, parts, here, render_md):
    # The second search group: every entry in the tree, findable by name or by path.
    return [[here + quote(e.name) + "/", "/".join([*parts, e.name]) + "/", ""] if e.is_dir() else
            [here + quote(e.name) + (".html" if render_md and is_markdown(e) else ""),
             "/".join([*parts, e.name]), human_size(e.stat().st_size)] for e in entries]

def write_search_index(out_root, index):
    # A .js file assigning a global, not .json fetched with fetch(): a script tag is not subject to
    # CORS, so search keeps working when the built site is opened straight off disk.
    (out_root / SEARCH_INDEX_NAME).write_text(
        "window.__rwvSearch = " + json.dumps(index, separators=(",", ":"), ensure_ascii=False) + ";\n", encoding="utf-8")

# --------------------------------------------------------------------- page assembly
def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

def breadcrumb_html(labels, depth):
    # labels: the root title, then one per path component, the last of them being this page. depth is how many
    # folders separate the page from the root — a file page sits in its folder, so it shares that folder's links.
    out = []
    for i, label in enumerate(labels):
        esc, up = html.escape(label), depth - i
        out.append(f'<span class="here">{esc}</span>' if i == len(labels) - 1
                   else f'<a href="{"../" * up if up else "./"}">{esc}</a>')
    return '<span class="sep">/</span>'.join(out)

def listing_rows(entries, render_md=False):
    rows = []
    for entry in entries:
        name = html.escape(entry.name)
        if entry.is_dir():
            rows.append(f'<tr><td class="icon">\U0001F4C1</td>'
                        f'<td class="name"><a href="{quote(entry.name)}/">{name}/</a></td><td class="size"></td></tr>')
        else:
            href = quote(entry.name) + (".html" if render_md and is_markdown(entry) else "")
            rows.append(f'<tr><td class="icon">\U0001F4C4</td>'
                        f'<td class="name"><a href="{href}">{name}</a></td>'
                        f'<td class="size">{human_size(entry.stat().st_size)}</td></tr>')
    return "\n".join(rows)

def footer_note_html(note, url):
    # An optional stamp for the footer — a deploy step puts the commit it built here. The note is escaped, so it is text and nothing else;
    # the link is the only markup added, and its URL comes from the command line (i.e. from whoever runs the build).
    if not note:
        return ""
    label = html.escape(note)
    return " · " + (f'<a href="{html.escape(url)}">{label}</a>' if url else label)

def build_page(labels, depth, body_html, entries, note_html="", render_md=False, search=False):
    crumbs = breadcrumb_html(labels, depth)
    body_block = f'<article class="markdown-body">\n{body_html}\n</article>\n' if body_html else ""
    rows = listing_rows(entries, render_md) or '<tr><td class="icon">—</td><td class="name">empty folder</td><td></td></tr>'
    here = html.escape(" / ".join(labels))
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{here}</title>\n<style>\n{STYLE}\n</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n<nav class="breadcrumb"><span class="crumbs">{crumbs}</span>'
        f'{SEARCH_BUTTON if search else ""}</nav>\n{body_block}'
        f'<section class="listing"><h2 class="listing-title">Contents</h2>\n'
        f"<table><tbody>\n{rows}\n</tbody></table></section>\n"
        f'<footer>Generated by repo-web-view{note_html}</footer>\n</div>\n'
        f'{search_ui("../" * depth or "./") if search else ""}</body>\n</html>\n'
    )

# --------------------------------------------------------------------- tree walk / generate
def find_readme(dir_path):
    return next((c for c in dir_path.iterdir() if c.is_file() and c.name.lower() == README_NAME), None)

def is_markdown(path):
    return path.is_file() and path.suffix.lower() == MD_SUFFIX

def list_entries(dir_path, extra_skip=()):
    # Generated pages are part of the site, not of the tree being listed: index.html, the search index, and
    # any NAME.md.html that belongs to a NAME.md sitting beside it.
    entries = [e for e in dir_path.iterdir() if not e.name.startswith(".") and e.name != INDEX_NAME
               and e.name not in extra_skip
               and not (e.name.lower().endswith(MD_PAGE_SUFFIX) and (dir_path / e.name[:-len(".html")]).is_file())]
    return sorted(entries, key=lambda e: (e.is_file(), e.name.lower()))

def generate(out_root, title, md, note_html="", render_md=False, search=True):
    pages, index = 0, {"pages": [], "files": []}
    for d in [out_root, *(p for p in out_root.rglob("*") if p.is_dir())]:
        # read before anything is written into d, so a page never lists itself
        entries = list_entries(d, (SEARCH_INDEX_NAME,) if search and d == out_root else ())
        parts = list(d.relative_to(out_root).parts)
        here = "".join(quote(part) + "/" for part in parts)   # this folder, relative to the site root
        readme = find_readme(d)
        readme_html = render_markdown(readme, md, render_md) if readme else ""
        (d / INDEX_NAME).write_text(build_page([title, *parts], len(parts), readme_html, entries, note_html, render_md, search),
                                    encoding="utf-8")
        pages += 1
        if search:
            index["files"] += file_records(entries, parts, here, render_md)
            if readme:
                index["pages"] += index_sections(readme_html, here, " / ".join([title, *parts]))
        if render_md:
            for f in (e for e in entries if is_markdown(e)):
                f_html = render_markdown(f, md, True)
                page = build_page([title, *parts, f.name], len(parts), f_html, entries, note_html, True, search)
                f.with_name(f.name + ".html").write_text(page, encoding="utf-8")   # FILE.md -> FILE.md.html, beside it
                pages += 1
                if search and f.name.lower() != README_NAME:   # a README already went in at its folder's page
                    index["pages"] += index_sections(f_html, here + quote(f.name) + ".html",
                                                     " / ".join([title, *parts, f.name]))
    if search:
        write_search_index(out_root, index)
    return pages

def copy_tree(src, out, excludes):
    def ignore(_dir, names):
        skip = {n for n in names if n.startswith(".")}
        for pat in excludes:
            skip |= set(fnmatch.filter(names, pat))
        return skip
    shutil.copytree(src, out, ignore=ignore)

HTACCESS = r"""# Generated by repo-web-view.
# Serve this tree as static files: plain files (scripts included) download, while
# folders (index.html) and .html tools render. Nothing here is executed server-side.
# Needs mod_headers, and AllowOverride FileInfo Indexes on this directory in the vhost.
<IfModule mod_headers.c>
  <FilesMatch ".">
    Header set Content-Disposition "attachment"
  </FilesMatch>
  <FilesMatch "(?i)\.html?$">
    Header set Content-Disposition "inline"
  </FilesMatch>
</IfModule>

# Download scripts instead of running them (PHP-FPM, CGI, ...); serve them statically.
RemoveHandler .php .phtml .phps .php3 .php4 .php5 .php7 .php8 .cgi .fcgi .pl .py .rb .lua .sh .shtml
RemoveType .php .phtml .phps
<FilesMatch "(?i)\.(php[0-9]?|phtml|phps|phar|cgi|fcgi|pl|py|rb|lua|sh|shtml)$">
  SetHandler default-handler
</FilesMatch>

DirectoryIndex index.html
"""

# --------------------------------------------------------------------- local preview server
def serve(out, port):
    import functools, http.server, socketserver
    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            path = self.translate_path(self.path)
            if os.path.isfile(path) and not path.lower().endswith((".html", ".htm")):
                self.send_header("Content-Disposition", "attachment")  # mirror the .htaccess in preview
            super().end_headers()
    with socketserver.TCPServer(("", port), functools.partial(Handler, directory=str(out))) as httpd:
        print(f"Serving {out} at http://localhost:{port}/  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")

# --------------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish a directory tree as a static, GitHub-style browsable site.")
    ap.add_argument("source", help="directory to publish (e.g. a repo checkout)")
    ap.add_argument("output", help="directory to write the generated site into (must be outside SOURCE)")
    ap.add_argument("--title", help="name for the root breadcrumb (default: the source folder name)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="filename glob to skip (repeatable); dotfiles are always skipped")
    ap.add_argument("--footer-note", default="", metavar="TEXT",
                    help="text to append to every page's footer, e.g. the commit hash a deploy built (empty: no note)")
    ap.add_argument("--footer-note-url", default="", metavar="URL",
                    help="make --footer-note a link to this URL, e.g. that commit on GitHub")
    ap.add_argument("--render-markdown", action="store_true",
                    help="give every .md file a rendered page (NAME.md.html) and link to it, instead of downloading it")
    ap.add_argument("--no-search", action="store_true",
                    help="leave out the search box and the search index it loads")
    ap.add_argument("--no-htaccess", action="store_true", help="do not write the force-download .htaccess")
    ap.add_argument("--force", action="store_true", help="overwrite OUTPUT if it already exists")
    ap.add_argument("--serve", nargs="?", type=int, const=8000, metavar="PORT",
                    help="after building, serve OUTPUT with production-like download headers (default port 8000)")
    args = ap.parse_args(argv)

    src, out = Path(args.source).resolve(), Path(args.output).resolve()
    if not src.is_dir():
        ap.error(f"source is not a directory: {src}")
    if out == src or src in out.parents or out in src.parents:
        ap.error("OUTPUT and SOURCE must not be the same or nested inside each other")
    if out.exists():
        if not args.force:
            ap.error(f"output already exists: {out} (use --force to overwrite)")
        shutil.rmtree(out)

    copy_tree(src, out, args.exclude)
    n = generate(out, args.title or src.name or "root", make_markdown(),
                 footer_note_html(args.footer_note.strip(), args.footer_note_url.strip()), args.render_markdown,
                 not args.no_search)
    if not args.no_htaccess:
        (out / HTACCESS_NAME).write_text(HTACCESS, encoding="utf-8")
    print(f"Generated {n} page(s) into {out}")
    if args.serve is not None:
        serve(out, args.serve)

# --------------------------------------------------------------------- search UI (inlined into every page)
SEARCH_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.68 11.74a6 6 0 0 1-7.922-8.982 6 6 0 0 1 8.982 '
               '7.922l3.04 3.04a.749.749 0 0 1-1.06 1.06ZM11.5 7a4.5 4.5 0 1 0-9 0 4.5 4.5 0 0 0 9 0Z"/></svg>')

SEARCH_BUTTON = ('<button type="button" class="search-open" aria-label="Search">'
                 f'{SEARCH_ICON}<span class="search-open-label">Search</span><kbd>/</kbd></button>')

SEARCH_MODAL = ('<div class="search-modal" hidden>\n'
                '<div class="search-panel" role="dialog" aria-modal="true" aria-label="Search">\n'
                f'<div class="search-head">{SEARCH_ICON}'
                '<input type="search" class="search-input" placeholder="Search" autocomplete="off" spellcheck="false">'
                '<button type="button" class="search-close" aria-label="Close">✕</button></div>\n'
                '<div class="search-status"></div>\n<div class="search-results"></div>\n</div>\n</div>\n')

SEARCH_JS = r"""
const SNIPPET = 200;      // how much of a matching section a result shows (all of it is searched)
const MAX_PAGES = 20;     // pages listed, each folding its own extra sections behind a "more" link
const MAX_FILES = 20;
const modal = document.querySelector(".search-modal");
const input = modal.querySelector(".search-input");
const list = modal.querySelector(".search-results");
const status = modal.querySelector(".search-status");
let data = null, pending = null, hits = [], active = -1;

// One shared index for the whole site, pulled in the first time someone searches. It is a script rather
// than a fetch on purpose: script tags are not subject to CORS, so this also works over file://.
function load() {
  if (data) return Promise.resolve(data);
  if (!pending) pending = new Promise((resolve) => {
    const el = document.createElement("script");
    el.src = ROOT + "search-index.js";
    el.onload = el.onerror = () => {
      const raw = window.__rwvSearch || { pages: [], files: [] };
      data = {
        pages: raw.pages.map(([loc, title, section, text]) => ({ loc, title, section, text,
          fields: [[title.toLowerCase(), 3], [section.toLowerCase(), 6], [text.toLowerCase(), 1]] })),
        files: raw.files.map(([href, path, size]) => ({ href, path, size,
          fields: [[path.toLowerCase(), 4], [(path.split("/").filter(Boolean).pop() || "").toLowerCase(), 8]] })),
      };
      resolve(data);
    };
    document.head.appendChild(el);
  });
  return pending;
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const esc = (s) => s.replace(/[&<>"]/g, (c) => ESCAPES[c]);
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function mark(text, terms) {
  const out = esc(text);
  return out && out.replace(new RegExp("(" + terms.map((t) => reEsc(esc(t))).join("|") + ")", "gi"), "<mark>$1</mark>");
}

// Every term has to turn up somewhere in the record; a term starting a word counts double, and the field it
// landed in sets the weight, so a heading hit outranks the same word buried in a paragraph.
function score(rec, terms) {
  let total = 0;
  for (const term of terms) {
    let best = 0;
    for (const [text, weight] of rec.fields) {
      const at = text.indexOf(term);
      if (at >= 0) best = Math.max(best, weight * (at === 0 || !/[a-z0-9]/.test(text[at - 1]) ? 2 : 1));
    }
    if (!best) return 0;
    total += best;
  }
  return total;
}

function rank(records, terms) {
  return records.map((rec) => [score(rec, terms), rec]).filter(([s]) => s > 0)
    .sort((a, b) => b[0] - a[0]).map(([, rec]) => rec);
}

// Sections of one page collapse into a single result — its best section, with the rest behind a "more"
// link — so that one long README cannot fill the whole list with itself. Ranked order is kept, so a page
// arrives at the position its best section earned.
function byPage(records) {
  const groups = new Map();
  for (const rec of records) {
    const page = rec.loc.split("#")[0];
    if (!groups.has(page)) groups.set(page, []);
    groups.get(page).push(rec);
  }
  return [...groups.values()];
}

// withTitle is off for the sections folded under a page: they all sit under the same one.
function pageHit(rec, terms, withTitle) {
  const title = withTitle ? `<span class="hit-title">\u{1F4C4} ${mark(rec.title, terms)}</span>` : "";
  const section = rec.section ? `<span class="hit-section">${mark(rec.section, terms)}</span>` : "";
  const cut = rec.text.slice(0, SNIPPET).replace(/[\s.,;:]+$/, "");
  const text = rec.text.length > SNIPPET ? cut + "\u2026" : rec.text;
  return `<a class="hit" href="${esc(ROOT + rec.loc)}">${title}${section}`
    + `<span class="hit-text">${mark(text, terms)}</span></a>`;
}

function pageGroup(group, terms, id) {
  const [best, ...rest] = group;
  if (!rest.length) return pageHit(best, terms, true);
  const label = `${rest.length} more on this page`;
  const extra = rest.map((rec) => pageHit(rec, terms, !rec.section)).join("");
  return pageHit(best, terms, true) + `<button type="button" class="hit-more" data-more="${id}">${label}</button>`
    + `<div class="hit-extra" id="${id}" hidden>${extra}</div>`;
}

function fileHit(rec, terms) {
  return `<a class="hit hit-file" href="${esc(ROOT + rec.href)}">`
    + `<span class="hit-icon">${rec.path.endsWith("/") ? "\u{1F4C1}" : "\u{1F4C4}"}</span>`
    + `<span class="hit-path">${mark(rec.path, terms)}</span><span class="hit-size">${esc(rec.size)}</span></a>`;
}

function render() {
  const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
  active = -1;
  if (!terms.length) {
    list.innerHTML = status.textContent = "";
  } else {
    const pages = rank(data.pages, terms), files = rank(data.files, terms), n = pages.length + files.length;
    const groups = byPage(pages).slice(0, MAX_PAGES);
    status.textContent = n ? `${n} matching result${n === 1 ? "" : "s"}` : "No results";
    list.innerHTML = (groups.length ? '<h3 class="search-group">Pages</h3>'
        + groups.map((g, i) => pageGroup(g, terms, `more-${i}`)).join("") : "")
      + (files.length ? '<h3 class="search-group">Files</h3>'
        + files.slice(0, MAX_FILES).map((rec) => fileHit(rec, terms)).join("") : "");
  }
  collect();
}

// Folded-away sections are in the DOM but must stay out of the keyboard walk until they are shown.
function collect() {
  hits = [...list.querySelectorAll(".hit")].filter((hit) => !hit.closest("[hidden]"));
  active = hits.findIndex((hit) => hit.classList.contains("active"));
}

function move(step) {
  if (!hits.length) return;
  if (active >= 0) hits[active].classList.remove("active");
  active = (active + step + hits.length) % hits.length;
  hits[active].classList.add("active");
  hits[active].scrollIntoView({ block: "nearest" });
}

function openSearch() {
  modal.hidden = false;
  document.body.classList.add("search-on");
  input.focus();
  input.select();
  load().then(render);
}

function closeSearch() {
  modal.hidden = true;
  document.body.classList.remove("search-on");
}

document.querySelector(".search-open").addEventListener("click", openSearch);
modal.querySelector(".search-close").addEventListener("click", closeSearch);
modal.addEventListener("click", (e) => { if (e.target === modal) closeSearch(); });
input.addEventListener("input", () => { if (data) render(); });
list.addEventListener("click", (e) => {
  const more = e.target.closest(".hit-more");
  if (!more) return;
  document.getElementById(more.dataset.more).hidden = false;
  more.remove();
  collect();
});

document.addEventListener("keydown", (e) => {
  if (modal.hidden) {
    const typing = /^(input|textarea|select)$/i.test(e.target.tagName) || e.target.isContentEditable;
    if (!typing && (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k"))) {
      e.preventDefault();
      openSearch();
    }
  } else if (e.key === "Escape") closeSearch();
  else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (e.key === "Enter" && active >= 0) { e.preventDefault(); hits[active].click(); }
});
"""

def search_ui(root):
    # root is this page's way back to the site root ("../../", or "./" at the top), so one index serves every page.
    return SEARCH_MODAL + f'<script type="module">\nconst ROOT = "{root}";\n{SEARCH_JS}</script>\n'

# --------------------------------------------------------------------- styles (inlined into every page)
CSS = """
:root {
  --fg: #1f2328; --muted: #59636e; --link: #0969da;
  --border: #d1d9e0; --bg: #ffffff; --canvas: #f6f8fa; --code-bg: #eff1f3;
  --mark-bg: #fff3c4; --mark-fg: #953800;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6edf3; --muted: #9198a1; --link: #4493f8;
    --border: #3d444d; --bg: #0d1117; --canvas: #151b23; --code-bg: #262c36;
    --mark-bg: #4a3410; --mark-fg: #f0b72f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1012px; margin: 0 auto; padding: 24px 16px 64px; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.breadcrumb { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.crumbs { flex: 1; min-width: 0; word-break: break-word; }
.breadcrumb .sep { color: var(--muted); margin: 0 6px; }
.breadcrumb .here { font-weight: 600; }
.markdown-body {
  border: 1px solid var(--border); border-radius: 6px;
  padding: 24px 28px; margin-bottom: 24px; overflow-wrap: break-word;
}
.markdown-body > :first-child { margin-top: 0; }
.markdown-body > :last-child { margin-bottom: 0; }
.markdown-body h1, .markdown-body h2, .markdown-body h3,
.markdown-body h4, .markdown-body h5, .markdown-body h6 {
  margin: 24px 0 16px; font-weight: 600; line-height: 1.25;
}
.markdown-body h1, .markdown-body h2 { padding-bottom: .3em; border-bottom: 1px solid var(--border); }
.markdown-body h1 { font-size: 2em; }
.markdown-body h2 { font-size: 1.5em; }
.markdown-body p, .markdown-body ul, .markdown-body ol, .markdown-body blockquote { margin: 0 0 16px; }
.markdown-body ul, .markdown-body ol { padding-left: 2em; }
.markdown-body blockquote { color: var(--muted); border-left: .25em solid var(--border); padding: 0 1em; }
.markdown-body code {
  background: var(--code-bg); padding: .2em .4em; border-radius: 6px; font-size: 85%;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
}
.markdown-body pre {
  background: var(--canvas); padding: 16px; border-radius: 6px; overflow: auto; margin: 0 0 16px;
}
.markdown-body pre code { background: transparent; padding: 0; font-size: 100%; }
.markdown-body img { max-width: 100%; }
.markdown-body table { border-collapse: collapse; margin: 0 0 16px; display: block; overflow: auto; }
.markdown-body th, .markdown-body td { border: 1px solid var(--border); padding: 6px 13px; }
.markdown-body tr:nth-child(2n) { background: var(--canvas); }
.markdown-body hr { border: 0; border-top: 1px solid var(--border); margin: 24px 0; }
.listing-title { font-size: 1.2em; font-weight: 600; margin: 0 0 12px; }
.listing table { width: 100%; border-collapse: collapse; border: 1px solid var(--border); border-radius: 6px; }
.listing td { border-top: 1px solid var(--border); padding: 8px 16px; }
.listing tr:first-child td { border-top: 0; }
.listing .icon { width: 1.5em; text-align: center; }
.listing .size { text-align: right; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
footer { margin-top: 24px; color: var(--muted); font-size: 12px; text-align: center; }
.search-open {
  display: flex; align-items: center; gap: 6px; flex: none; cursor: pointer;
  padding: 3px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--canvas); color: var(--muted); font: inherit; font-size: 14px;
}
.search-open:hover { border-color: var(--muted); }
.search-open svg { width: 14px; height: 14px; fill: currentColor; }
.search-open kbd { padding: 0 4px; border: 1px solid var(--border); border-radius: 4px; font: inherit; font-size: 11px; }
.search-modal { position: fixed; inset: 0; z-index: 20; padding: 0 16px; background: rgba(0, 0, 0, .45); }
.search-modal[hidden] { display: none; }
.search-panel {
  display: flex; flex-direction: column; max-width: 700px; max-height: 85vh; margin: 0 auto;
  background: var(--bg); border: 1px solid var(--border); border-top: 0; border-radius: 0 0 6px 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, .25);
}
.search-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.search-head svg { flex: none; width: 18px; height: 18px; fill: var(--muted); }
.search-input {
  flex: 1; min-width: 0; border: 0; outline: none;
  background: transparent; color: var(--fg); font: inherit; font-size: 16px;
}
.search-input::-webkit-search-cancel-button { display: none; }
.search-close { padding: 0 4px; border: 0; background: transparent; color: var(--muted); font-size: 20px; cursor: pointer; }
.search-status { padding: 6px 16px; background: var(--canvas); color: var(--muted); font-size: 13px; }
.search-status:empty { display: none; }
.search-results { overflow: auto; }
.search-group {
  margin: 0; padding: 12px 16px 4px; color: var(--muted);
  font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
}
.hit { display: block; padding: 10px 16px; border-top: 1px solid var(--border); color: var(--fg); }
.hit:hover, .hit.active { background: var(--canvas); text-decoration: none; }
.hit-title { display: block; color: var(--muted); font-size: 12px; }
.hit-section { display: block; font-weight: 600; }
.hit-text { display: block; margin-top: 2px; color: var(--muted); font-size: 14px; }
.hit-more {
  display: block; width: 100%; padding: 0 16px 10px; border: 0; cursor: pointer;
  background: transparent; color: var(--link); font: inherit; font-size: 13px; text-align: left;
}
.hit-more:hover { text-decoration: underline; }
.hit-extra { background: var(--canvas); }
.hit-extra .hit { padding-left: 32px; }
.hit-file { display: flex; align-items: center; gap: 8px; }
.hit-path { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.hit-size { color: var(--muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
mark { background: var(--mark-bg); color: var(--mark-fg); border-radius: 2px; }
body.search-on { overflow: hidden; }
@media (max-width: 600px) {
  .search-open-label, .search-open kbd { display: none; }
}
"""
# Pygments token colours (drop its container-background rule so our own pre/code bg wins).
PYG_CSS = "\n".join(l for l in HtmlFormatter(style="default").get_style_defs(".hl").splitlines()
                    if not l.startswith(".hl {"))
STYLE = CSS + "\n" + PYG_CSS

if __name__ == "__main__":
    sys.exit(main())
