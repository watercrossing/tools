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

A generated .htaccess makes Apache force `Content-Disposition: attachment` on every
file except those index pages, so clicking any file downloads it while folders render.

Run (deps auto-installed from the inline metadata above):
    uv run repo-web-view.py SOURCE OUTPUT            # or ./repo-web-view.py SOURCE OUTPUT
    uv run repo-web-view.py . ../site --serve        # build, then preview at http://localhost:8000

SOURCE is copied into OUTPUT (dotfiles skipped) and an index.html is generated in every
folder. OUTPUT must not be the same as, or nested inside, SOURCE. Use --force to overwrite
a non-empty OUTPUT (what a deploy step wants).
"""
import argparse, base64, fnmatch, html, mimetypes, os, re, shutil, sys
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

# --------------------------------------------------------------------- page assembly
def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

def breadcrumb_html(title, parts):
    n = len(parts)
    items = [(title, "../" * n if n else "./")]
    items += [(part, "../" * (n - i - 1) if n - i - 1 else "./") for i, part in enumerate(parts)]
    out = []
    for i, (label, href) in enumerate(items):
        esc = html.escape(label)
        out.append(f'<span class="here">{esc}</span>' if i == len(items) - 1 else f'<a href="{href}">{esc}</a>')
    return '<span class="sep">/</span>'.join(out)

def listing_rows(entries):
    rows = []
    for entry in entries:
        name = html.escape(entry.name)
        if entry.is_dir():
            rows.append(f'<tr><td class="icon">\U0001F4C1</td>'
                        f'<td class="name"><a href="{quote(entry.name)}/">{name}/</a></td><td class="size"></td></tr>')
        else:
            rows.append(f'<tr><td class="icon">\U0001F4C4</td>'
                        f'<td class="name"><a href="{quote(entry.name)}">{name}</a></td>'
                        f'<td class="size">{human_size(entry.stat().st_size)}</td></tr>')
    return "\n".join(rows)

def footer_note_html(note, url):
    # An optional stamp for the footer — a deploy step puts the commit it built here. The note is escaped, so it is text and nothing else;
    # the link is the only markup added, and its URL comes from the command line (i.e. from whoever runs the build).
    if not note:
        return ""
    label = html.escape(note)
    return " · " + (f'<a href="{html.escape(url)}">{label}</a>' if url else label)

def build_page(title, parts, readme_html, entries, note_html=""):
    crumbs = breadcrumb_html(title, parts)
    readme_block = f'<article class="markdown-body">\n{readme_html}\n</article>\n' if readme_html else ""
    rows = listing_rows(entries) or '<tr><td class="icon">—</td><td class="name">empty folder</td><td></td></tr>'
    here = html.escape(" / ".join([title, *parts]))
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{here}</title>\n<style>\n{STYLE}\n</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n<nav class="breadcrumb">{crumbs}</nav>\n{readme_block}'
        f'<section class="listing"><h2 class="listing-title">Contents</h2>\n'
        f"<table><tbody>\n{rows}\n</tbody></table></section>\n"
        f'<footer>Generated by repo-web-view{note_html}</footer>\n</div>\n</body>\n</html>\n'
    )

# --------------------------------------------------------------------- tree walk / generate
def find_readme(dir_path):
    return next((c for c in dir_path.iterdir() if c.is_file() and c.name.lower() == README_NAME), None)

def list_entries(dir_path):
    entries = [e for e in dir_path.iterdir() if not e.name.startswith(".") and e.name != INDEX_NAME]
    return sorted(entries, key=lambda e: (e.is_file(), e.name.lower()))

def generate(out_root, title, md, note_html=""):
    dirs = [out_root, *(p for p in out_root.rglob("*") if p.is_dir())]
    for d in dirs:
        readme = find_readme(d)
        readme_html = inline_images(md.render(readme.read_text("utf-8", "replace")), d) if readme else ""
        parts = list(d.relative_to(out_root).parts)
        (d / INDEX_NAME).write_text(build_page(title, parts, readme_html, list_entries(d), note_html), encoding="utf-8")
    return len(dirs)

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
                 footer_note_html(args.footer_note.strip(), args.footer_note_url.strip()))
    if not args.no_htaccess:
        (out / HTACCESS_NAME).write_text(HTACCESS, encoding="utf-8")
    print(f"Generated {n} page(s) into {out}")
    if args.serve is not None:
        serve(out, args.serve)

# --------------------------------------------------------------------- styles (inlined into every page)
CSS = """
:root {
  --fg: #1f2328; --muted: #59636e; --link: #0969da;
  --border: #d1d9e0; --bg: #ffffff; --canvas: #f6f8fa; --code-bg: #eff1f3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6edf3; --muted: #9198a1; --link: #4493f8;
    --border: #3d444d; --bg: #0d1117; --canvas: #151b23; --code-bg: #262c36;
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
.breadcrumb { margin-bottom: 16px; word-break: break-word; }
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
"""
# Pygments token colours (drop its container-background rule so our own pre/code bg wins).
PYG_CSS = "\n".join(l for l in HtmlFormatter(style="default").get_style_defs(".hl").splitlines()
                    if not l.startswith(".hl {"))
STYLE = CSS + "\n" + PYG_CSS

if __name__ == "__main__":
    sys.exit(main())
