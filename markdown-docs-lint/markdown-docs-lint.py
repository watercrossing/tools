#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Lint a tree of Markdown docs for the failures that break silently: dead relative links, dead #anchors, unreachable pages, oversized files.

Written for a documentation tree that is navigated by links and by grep rather than rendered as a website, and that is maintained partly by LLM
agents. Those two facts set the priorities. An agent that follows a link into a missing file has no fallback and no human to ask, and renaming a
heading silently invalidates every #anchor pointing at it -- nothing errors, nothing looks different in an editor, and the break surfaces only
when somebody clicks it.

The fiddly part, and the reason this is a tool rather than ten lines of shell, is the heading -> anchor slug. GitHub lowercases the heading,
drops inline markup, deletes every character that is not alphanumeric/space/hyphen/underscore, then maps spaces to hyphens. So an em dash
surrounded by spaces leaves *two* hyphens behind: "## `m` -- Minden (kiel.m.ibecker.eu)" becomes #m--minden-kielmibeckereu. A reimplementation
that collapses runs of whitespace reports a batch of false positives, which is worse than having no checker, because it teaches you to ignore
the output.

Fenced code blocks and inline code spans are blanked before links are extracted, so a `[x](y)` in a shell example is not mistaken for a link and
a "# comment" inside a fence is not mistaken for a heading. Blanking preserves line numbers, so diagnostics still point at the right line.
"""
import argparse, fnmatch, re, sys, urllib.parse
from collections import deque
from pathlib import Path

FENCE_RE = re.compile(r'\s*(`{3,}|~{3,})')
ATX_RE = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
INLINE_CODE_RE = re.compile(r'`+[^`]*`+')
LINK_RE = re.compile(r'(?<!!)\[[^\]]*\]\(\s*<?([^\s)>]+)')
SKIP_SCHEME_RE = re.compile(r'^(?:[a-z][a-z0-9+.-]*:|//|#?$)', re.I)  # http:, mailto:, protocol-relative, and the empty target


def strip_code(text, inline=True):
    """Blank fenced blocks (and optionally inline spans), keeping one output line per input line so line numbers stay usable.

    Headings need inline spans left alone: GitHub keeps the *text* inside backticks when it builds a slug, so blanking `m` would lose it."""
    out, fence = [], None
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append("")
            else:
                out.append(INLINE_CODE_RE.sub(lambda c: " " * len(c.group(0)), line) if inline else line)
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append("")
    return out


def slug(text):
    """GitHub's heading -> anchor transform. Note the deliberate absence of whitespace collapsing; see the module docstring."""
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)  # a link in a heading contributes its text only
    text = re.sub(r'[`*~]', '', text).lower()               # inline markup vanishes; '_' is kept, it survives into real GitHub slugs
    return "".join(c for c in text if c.isalnum() or c in " -_").replace(" ", "-")


def anchors(text):
    """Every #anchor the file defines, with GitHub's -1/-2 suffixes for repeated headings."""
    seen, out = {}, set()
    for line in strip_code(text, inline=False):
        if m := ATX_RE.match(line):
            base = slug(m.group(2))
            out.add(base if base not in seen else f"{base}-{seen[base]}")
            seen[base] = seen.get(base, 0) + 1
    return out


def links(text):
    """(line number, raw target) for every inline link outside code."""
    return [(n, m.group(1)) for n, line in enumerate(strip_code(text), 1) for m in LINK_RE.finditer(line)]


def main():
    p = argparse.ArgumentParser(description="Lint a Markdown docs tree for dead links, dead anchors, unreachable pages and oversized files.")
    p.add_argument("root", nargs="?", default=".", help="directory to scan (default: current)")
    p.add_argument("--entry", action="append", default=[], help="reachability entry point, repeatable (default: README.md)")
    p.add_argument("--max-lines", type=int, default=150, help="hard cap; over this is an error (default: 150, 0 disables)")
    p.add_argument("--warn-lines", type=int, default=100, help="soft cap; over this is a warning (default: 100, 0 disables)")
    p.add_argument("--exempt", action="append", default=[], help="glob, relative to root, exempt from the size caps; repeatable")
    p.add_argument("--no-orphans", action="store_true", help="skip the unreachable-page check")
    p.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    p.add_argument("-q", "--quiet", action="store_true", help="print problems only, no summary")
    args = p.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")
    files = sorted(f for f in root.rglob("*.md") if ".git" not in f.parts)
    if not files:
        sys.exit(f"no .md files under {root}")
    text = {f: f.read_text(encoding="utf-8", errors="replace") for f in files}
    anchor_cache = {f: anchors(t) for f, t in text.items()}
    rel = lambda f: str(f.relative_to(root))

    problems, graph = [], {f: set() for f in files}
    for f in files:
        for lineno, raw in links(text[f]):
            target = urllib.parse.unquote(raw.rstrip('"\''))
            if SKIP_SCHEME_RE.match(target) and not target.startswith("#"):
                continue
            path, _, frag = target.partition("#")
            if path.startswith("/"):
                continue  # site-root-relative; meaningless without knowing the publish root
            dest = (f.parent / path).resolve() if path else f
            if path and not dest.exists():
                problems.append((rel(f), lineno, "error", "link", f"missing file: {target}"))
                continue
            if dest.suffix == ".md" and dest in graph:
                graph[f].add(dest)
            if frag and dest.suffix == ".md":
                if dest not in anchor_cache:
                    anchor_cache[dest] = anchors(dest.read_text(encoding="utf-8", errors="replace"))
                if frag not in anchor_cache[dest]:
                    problems.append((rel(f), lineno, "error", "anchor", f"no such heading: {target}"))

    for f in files:
        n = len(text[f].splitlines())
        if any(fnmatch.fnmatch(rel(f), g) for g in args.exempt):
            continue
        if args.max_lines and n > args.max_lines:
            problems.append((rel(f), n, "error", "size", f"{n} lines, over the {args.max_lines}-line cap -- split it"))
        elif args.warn_lines and n > args.warn_lines:
            problems.append((rel(f), n, "warning", "size", f"{n} lines, over the {args.warn_lines}-line target"))

    if not args.no_orphans:
        entries = [root / e for e in (args.entry or ["README.md"])]
        if missing := [e for e in entries if e not in graph]:
            sys.exit("entry point not found: " + ", ".join(rel(e) for e in missing))
        seen, queue = set(entries), deque(entries)
        while queue:
            for nxt in graph[queue.popleft()] - seen:
                seen.add(nxt)
                queue.append(nxt)
        problems += [(rel(f), 1, "warning", "orphan", "not reachable by links from " + ", ".join(rel(e) for e in entries))
                     for f in files if f not in seen]

    for path, line, level, check, msg in sorted(problems, key=lambda x: (x[0], x[1])):
        print(f"{path}:{line}: {level}: [{check}] {msg}")
    errors = sum(1 for p in problems if p[2] == "error")
    if not args.quiet:
        warnings = len(problems) - errors
        print(f"\n{len(files)} files, {errors} error{'s' * (errors != 1)}, {warnings} warning{'s' * (warnings != 1)}", file=sys.stderr)
    return 1 if errors or (args.strict and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
