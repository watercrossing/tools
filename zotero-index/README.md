# zotero-index

Query a Zotero library offline, from a [Better BibTeX](https://retorque.re/zotero-better-bibtex/) auto-export.
No Zotero install, no web API, no API key: just the export file, e.g. synced to your other machines by a cloud drive.

```sh
zotero-index path smith_example_2020 --ext pdf     # where is the PDF?
zotero-index show smith_example_2020               # the record
zotero-index doi 10.1234/example.2020              # which citekey has this DOI?
zotero-index search usable security --year 2018-   # one line per hit: citekey  year  first-author  title
zotero-index check                                 # data-quality report
```

One file, standard library only, run with [uv](https://docs.astral.sh/uv/).

## Why

Zotero knows where every attachment lives, but only on the machine running Zotero, and only while it runs.
Better BibTeX can auto-export the whole library as JSON and keep that file updated; put it in a synced folder and every other machine has the library's metadata, collections and attachment paths too.
What it lacks is a way to *ask* it anything: the export is several MB of nested JSON, and its attachment paths are absolute paths on the exporting host (`C:\Users\alice\Sync\Zotero\sm\smith_example_2020.pdf`), useless as they stand on a Linux box where the same folder is `~/sync/Zotero`.

This tool reads the export, rewrites the paths for the host it runs on, and answers the questions you actually have: where is the file for this citekey, which citekey is this DOI, what's in this collection.

## Install

Symlink it onto your `PATH`; nothing in it depends on where it lives.

```sh
ln -s ~/src/tools/zotero-index/zotero-index.py ~/.local/bin/zotero-index
```

## Set up Better BibTeX

In Zotero: right-click *My Library* → *Export Library…* → format **BetterBibTeX JSON**, tick **Keep updated**, and save it somewhere synced.
Include notes if you like; they are skipped.
Better BibTeX then rewrites the file as the library changes, whenever Zotero is running.

## Configure

`$XDG_CONFIG_HOME/zotero-index/config.toml` (default `~/.config/zotero-index/config.toml`):

```toml
export = "~/sync/Zotero/library.json"

[path_map]   # prefix in the export = prefix to emit; longest match wins
'C:\Users\alice\Sync\Zotero' = "~/sync/Zotero"
'C:\Users\alice\Sync\Zotero\Archive' = "/mnt/archive/zotero"
```

- Use single-quoted TOML strings for Windows paths, so backslashes are literal.
- `~` is expanded in `export` and in the mapped-to prefixes; a relative `export` is relative to the config file.
- Prefixes match on whole path components (`…\Zotero` does not match `…\ZoteroOld`), case-insensitively for Windows paths.
  Windows paths come out with forward slashes.
- An attachment whose path matches no prefix is **unmapped**: `path` reports it on stderr rather than printing a path that doesn't exist here, and `check` lists them all.
  Typically these are files Zotero still keeps in its own `storage/` folder on the exporting machine.
- With no `[path_map]` at all, paths are passed through unchanged (forward slashes aside), for a library exported on the same machine.

`--config FILE` reads a different config; `--export FILE` overrides the export path, and with it the config becomes optional.
A missing config or export is a short error naming the path it expected.

## Commands

| Command | Prints | Exit 1 when |
|---|---|---|
| `path CITEKEY [--ext pdf]…` | attachment paths, one per line | no (mapped) attachment matches |
| `show CITEKEY` | the record: type, title, authors, year, DOI, tags, collections, attachments, abstract | no such citekey |
| `doi DOI` | the citekey(s) with that DOI (any case; a `https://doi.org/` or `doi:` prefix is fine) | no match |
| `search [TERMS…] [--collection C] [--tag T]… [--year Y]` | `citekey  year  first-author  title` per hit | no hits |
| `check` | citekeys shared by several items, attachment filenames that don't start with their citekey, items without attachments, unmapped paths | never |

- `search` terms are case-insensitive substrings, all of which must occur in the title, authors, tags or citekey.
  `--collection` takes a full path (`Teaching/Module A`) or any run of whole components (`Module A`), and includes subcollections.
  `--tag` is an exact, case-insensitive tag; repeat it to require several.
  `--year` takes `2020`, `2018-2020`, `2018-` or `-2020`.
- Every command takes `--json` for machine-readable output.
- A DOI is taken from the item's DOI field, or from a `DOI: …` line in *Extra* for item types without one.

**A citekey shared by several items is never resolved silently.**
`path` and `show` print every item with that key, list them on stderr, and exit **2**.
(Better BibTeX can produce shared keys, e.g. when an item was added twice; `check` lists them so you can merge the duplicates in Zotero.)

Exit status 3 means the config or the export is missing or unreadable.

## The index

Reading a multi-MB export on every call would be slow, especially if it sits on a network or FUSE mount, so the tool keeps a slim index in `$XDG_CACHE_HOME/zotero-index/` (default `~/.cache/zotero-index/`), one per export.

Every run stats the export and the config once and compares mtime and size with the values the index was built from.
If anything changed, it re-reads the export and rebuilds; otherwise the export is not opened at all.
The index is written to a temp file and renamed into place, so concurrent runs are safe.
There is no file watcher and nothing to schedule: the next query after the export changes picks it up.
Delete the cache directory to force a rebuild.

## Not in scope

Writing to Zotero, the Zotero web API, full-text search of attachments, watching files.

## Tests

```sh
uv run tests/test_zotero_index.py
```

They run against a small synthetic export, [`tests/export.json`](tests/export.json), which doubles as an example of the format.
