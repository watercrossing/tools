#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Query a Zotero library offline, from a Better BibTeX auto-export ("BetterBibTeX JSON", with "Keep updated" on).

No Zotero install, no web API, no API key: the export is the only input, e.g. synced to other machines by a cloud drive.
Attachment paths in the export are absolute paths on the exporting host, often Windows; a [path_map] in the config rewrites them for this one.

A slim index is cached per export under $XDG_CACHE_HOME/zotero-index/. Every run stats the export (and the config) once and rebuilds the index
only if mtime or size changed, so an export sitting on a slow FUSE mount is read only when it has actually changed. The index file's first line
is a small header holding those stat values, so a fresh index is detected without parsing the rest.

Exit status: 0 found, 1 nothing found, 2 a citekey is shared by several items (all are printed; none is picked), 3 config or export missing.
"""
import argparse, hashlib, json, os, re, sys, tempfile, tomllib
from pathlib import Path

INDEX_VERSION = 1
SKIP_TYPES = {"note", "attachment", "annotation"}
EXIT_NONE, EXIT_DUPLICATE, EXIT_SETUP = 1, 2, 3
WINDOWS_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')
DOI_RE = re.compile(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', re.I)
EXTRA_DOI_RE = re.compile(r'^DOI:\s*(\S+)', re.I | re.M)
YEAR_RE = re.compile(r'\b(\d{4})\b')


class SetupError(Exception):
    pass


# --- configuration ---------------------------------------------------------------------------------------------------------------------------

def default_config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "zotero-index" / "config.toml"


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "zotero-index"


def load_config(config_arg, export_arg):
    """Return (config path or None, export path, path_map). The config is optional only when --export names the export directly."""
    path = Path(config_arg).expanduser() if config_arg else default_config_path()
    if path.is_file():
        with path.open("rb") as f:
            try:
                cfg = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SetupError(f"{path}: {e}")
    elif config_arg or not export_arg:
        raise SetupError(f"no config at {path}; create it with export = \"/path/to/library.json\", or pass --export")
    else:
        path, cfg = None, {}
    export = export_arg or cfg.get("export")
    if not export:
        raise SetupError(f"{path}: no 'export' set; add export = \"/path/to/library.json\", or pass --export")
    export = Path(export).expanduser()
    if not export.is_absolute():
        export = (path.parent if path and not export_arg else Path.cwd()) / export
    path_map = {k: str(v) for k, v in (cfg.get("path_map") or {}).items()}
    return path, export.absolute(), path_map


# --- path mapping ----------------------------------------------------------------------------------------------------------------------------

def normalise(p):
    """Windows paths get forward slashes; POSIX paths are left alone (a backslash is a legal filename character there)."""
    return p.replace("\\", "/") if WINDOWS_RE.match(p) else p


def map_path(raw, path_map):
    """Rewrite an exported attachment path for this host: longest matching prefix wins, on a whole path component.

    Returns (path, mapped). Windows prefixes compare case-insensitively, as Windows does. With an empty path_map, paths pass through unchanged
    and count as mapped: there is nothing to translate."""
    p = normalise(raw)
    if not path_map:
        return p, True
    best = None
    for src, dst in path_map.items():
        s = normalise(src).rstrip("/")
        fold = (lambda x: x.lower()) if WINDOWS_RE.match(src) else (lambda x: x)
        if fold(p) == fold(s) or fold(p).startswith(fold(s) + "/"):
            if best is None or len(s) > len(best[0]):
                best = (s, dst)
    if best is None:
        return p, False
    dst = os.path.expanduser(best[1]).rstrip("/") or "/"
    rest = p[len(best[0]):].lstrip("/")
    return (f"{dst}/{rest}" if rest else dst).replace("//", "/"), True


# --- building the index ----------------------------------------------------------------------------------------------------------------------

def norm_doi(doi):
    return DOI_RE.sub("", doi.strip()).lower() if doi else ""


def creator_name(c):
    return c.get("name") or ", ".join(x for x in (c.get("lastName"), c.get("firstName")) if x)


def collection_paths(collections):
    """itemID -> sorted list of full collection paths ("A/B/C"). Membership is listed on the collection, not on the item."""
    def full(key, seen=()):
        c = collections[key]
        parent = c.get("parent")
        if parent and parent in collections and parent not in seen:
            return f"{full(parent, seen + (key,))}/{c['name']}"
        return c["name"]
    member = {}
    for key, c in collections.items():
        name = full(key)
        for item_id in c.get("items", []):
            member.setdefault(item_id, set()).add(name)
    return {k: sorted(v) for k, v in member.items()}


def build_records(export, path_map):
    members = collection_paths(export.get("collections") or {})
    records = []
    for it in export.get("items", []):
        if it.get("itemType") in SKIP_TYPES or not it.get("citationKey"):
            continue
        creators = it.get("creators") or []
        authors = [creator_name(c) for c in creators if c.get("creatorType", "author") == "author"] or [creator_name(c) for c in creators]
        doi = it.get("DOI") or next(iter(EXTRA_DOI_RE.findall(it.get("extra") or "")), "")
        year = YEAR_RE.search(it.get("date") or "")
        attachments = [dict(zip(("path", "mapped"), map_path(a["path"], path_map))) for a in it.get("attachments") or [] if a.get("path")]
        records.append({
            "citekey": it["citationKey"], "itemKey": it.get("itemKey") or it.get("key", ""), "type": it.get("itemType", ""),
            "title": it.get("title", ""), "authors": [a for a in authors if a], "year": year.group(1) if year else "", "doi": norm_doi(doi),
            "tags": [t["tag"] if isinstance(t, dict) else t for t in it.get("tags") or []], "collections": members.get(it.get("itemID"), []),
            "attachments": attachments, "abstract": it.get("abstractNote", ""),
        })
    return records


def stamp(path):
    if path is None:
        return None
    st = path.stat()
    return [str(path), st.st_mtime_ns, st.st_size]


def load_index(config_path, export_path, path_map):
    """Return the records, rebuilding the cached index if the export or config changed since it was written."""
    try:
        header = {"version": INDEX_VERSION, "export": stamp(export_path), "config": stamp(config_path)}
    except FileNotFoundError as e:
        where = f" (set in {config_path})" if config_path else ""
        raise SetupError(f"export not found: {e.filename}{where}; expected config at {config_path or default_config_path()}")
    cache = cache_dir() / f"{hashlib.sha256(str(export_path).encode()).hexdigest()[:16]}.json"
    try:
        with cache.open(encoding="utf-8") as f:
            if json.loads(f.readline()) == header:
                return json.loads(f.read())
    except (OSError, ValueError):
        pass
    try:
        with export_path.open(encoding="utf-8") as f:
            export = json.load(f)
    except OSError as e:
        raise SetupError(f"cannot read export {export_path}: {e.strerror}")
    except ValueError as e:
        raise SetupError(f"{export_path}: not a BetterBibTeX JSON export ({e})")
    records = build_records(export, path_map)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache.parent, prefix=".tmp-", suffix=".json", delete=False) as tmp:
        tmp.write(json.dumps(header) + "\n" + json.dumps(records, ensure_ascii=False))
    os.replace(tmp.name, cache)  # atomic, so a concurrent run sees either the old index or the new one, never half of one
    return records


# --- commands --------------------------------------------------------------------------------------------------------------------------------

def emit_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def emit(args, hits, line):
    if args.json:
        emit_json(hits)
    else:
        for r in hits:
            print(line(r))


def by_citekey(records, citekey):
    hits = [r for r in records if r["citekey"] == citekey]
    if len(hits) > 1:
        print(f"zotero-index: citekey {citekey} is shared by {len(hits)} items:", file=sys.stderr)
        for r in hits:
            print(f"  {r['itemKey']}  {r['title']}", file=sys.stderr)
    return hits


def status(hits, found=None):
    return EXIT_DUPLICATE if len(hits) > 1 else 0 if (hits if found is None else found) else EXIT_NONE


def cmd_path(records, args):
    hits = by_citekey(records, args.citekey)
    exts = {e.lower().lstrip(".") for e in args.ext or []}
    found = [dict(citekey=r["citekey"], itemKey=r["itemKey"], **a) for r in hits for a in r["attachments"]
             if not exts or Path(a["path"]).suffix.lower().lstrip(".") in exts]
    if args.json:
        emit_json(found)
    else:
        for a in found:
            if a["mapped"]:
                print(a["path"])
            else:
                print(f"zotero-index: unmapped: {a['path']}", file=sys.stderr)
    return status(hits, [a for a in found if a["mapped"]])


def show_text(r):
    rows = [("citekey", r["citekey"]), ("itemKey", r["itemKey"]), ("type", r["type"]), ("title", r["title"]),
            ("authors", "; ".join(r["authors"])), ("year", r["year"]), ("doi", r["doi"]), ("tags", "; ".join(r["tags"]))]
    rows += [("collections" if i == 0 else "", c) for i, c in enumerate(r["collections"])]
    rows += [("attachments" if i == 0 else "", a["path"] + ("" if a["mapped"] else "  (unmapped)")) for i, a in enumerate(r["attachments"])]
    rows += [("abstract", r["abstract"])]
    return "\n".join(f"{k:<12}{v}".rstrip() for k, v in rows if v or not k)


def cmd_show(records, args):
    hits = by_citekey(records, args.citekey)
    if args.json:
        emit_json(hits)
    elif hits:
        print("\n\n".join(map(show_text, hits)))
    return status(hits)


def one_line(r):
    return f"{r['citekey']}  {r['year'] or '-'}  {r['authors'][0] if r['authors'] else '-'}  {r['title']}"


def cmd_doi(records, args):
    hits = [r for r in records if r["doi"] and r["doi"] == norm_doi(args.doi)]
    emit(args, hits, lambda r: r["citekey"])
    return 0 if hits else EXIT_NONE


def year_match(spec):
    lo, _, hi = spec.partition("-")
    return lambda y: bool(y) and (lo or "0000") <= y <= (hi or ("9999" if _ else lo))


def cmd_search(records, args):
    terms = [t.lower() for t in args.terms]
    coll = f"/{args.collection.strip('/').lower()}/" if args.collection else None
    tags = {t.lower() for t in args.tag or []}
    years = year_match(args.year) if args.year else None
    hits = [r for r in records
            if all(t in "\n".join([r["title"], r["citekey"], *r["authors"], *r["tags"]]).lower() for t in terms)
            and (coll is None or any(coll in f"/{c.lower()}/" for c in r["collections"]))
            and tags <= {t.lower() for t in r["tags"]}
            and (years is None or years(r["year"]))]
    emit(args, hits, one_line)
    return 0 if hits else EXIT_NONE


def check_report(records):
    seen = {}
    for r in records:
        seen.setdefault(r["citekey"], []).append(r)
    return {
        "duplicate_citekeys": [{"citekey": k, "items": [{"itemKey": r["itemKey"], "title": r["title"]} for r in rs]}
                               for k, rs in sorted(seen.items()) if len(rs) > 1],
        "misnamed_attachments": [{"citekey": r["citekey"], "path": a["path"]} for r in records for a in r["attachments"]
                                 if not a["path"].rsplit("/", 1)[-1].startswith(r["citekey"])],
        "without_attachments": [{"citekey": r["citekey"], "itemKey": r["itemKey"], "title": r["title"]} for r in records if not r["attachments"]],
        "unmapped_paths": [{"citekey": r["citekey"], "path": a["path"]} for r in records for a in r["attachments"] if not a["mapped"]],
    }


def cmd_check(records, args):
    report = check_report(records)
    if args.json:
        emit_json(report)
        return 0
    sections = [("citekeys shared by several items", [f"{d['citekey']}\n" + "\n".join(f"  {i['itemKey']}  {i['title']}" for i in d["items"])
                                                     for d in report["duplicate_citekeys"]]),
                ("attachment filenames not starting with the citekey", [f"{m['citekey']}  {m['path']}" for m in report["misnamed_attachments"]]),
                ("items without attachments", [f"{n['citekey']}  {n['itemKey']}  {n['title']}" for n in report["without_attachments"]]),
                ("unmapped attachment paths", [f"{u['citekey']}  {u['path']}" for u in report["unmapped_paths"]])]
    print(f"{len(records)} items")
    for title, lines in sections:
        print(f"\n## {len(lines)} {title}", *lines, sep="\n")
    return 0


# --- CLI -------------------------------------------------------------------------------------------------------------------------------------

def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("--config", default=argparse.SUPPRESS, help=f"config file (default: {default_config_path()})")
    common.add_argument("--export", default=argparse.SUPPRESS, help="BetterBibTeX JSON export, overriding the config's 'export'")
    ap = argparse.ArgumentParser(prog="zotero-index", description=__doc__.split("\n\n")[0], parents=[common])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("path", parents=[common], help="attachment path(s) of an item, one per line")
    p.add_argument("citekey")
    p.add_argument("--ext", action="append", help="only attachments with this extension (repeatable), e.g. --ext pdf")
    sub.add_parser("show", parents=[common], help="the full record of an item").add_argument("citekey")
    sub.add_parser("doi", parents=[common], help="citekey(s) for a DOI").add_argument("doi")
    p = sub.add_parser("search", parents=[common], help="case-insensitive search of title, authors, tags and citekey (all terms must match)")
    p.add_argument("terms", nargs="*")
    p.add_argument("--collection", help="in this collection or below it; a full path (A/B) or any run of whole components (B)")
    p.add_argument("--tag", action="append", help="has this tag, case-insensitive (repeatable: all must match)")
    p.add_argument("--year", help="a year (2020) or a range (2018-2020, 2018-, -2020)")
    sub.add_parser("check", parents=[common], help="data-quality report on the library")
    args = ap.parse_args(argv)
    args.json, args.config, args.export = (getattr(args, k, d) for k, d in (("json", False), ("config", None), ("export", None)))
    if args.command == "search" and not (args.terms or args.collection or args.tag or args.year):
        ap.error("search needs at least one term or filter")
    try:
        records = load_index(*load_config(args.config, args.export))
    except SetupError as e:
        print(f"zotero-index: {e}", file=sys.stderr)
        return EXIT_SETUP
    return {"path": cmd_path, "show": cmd_show, "doi": cmd_doi, "search": cmd_search, "check": cmd_check}[args.command](records, args)


if __name__ == "__main__":
    sys.exit(main())
