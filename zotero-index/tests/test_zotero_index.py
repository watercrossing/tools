#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest"]
# ///
"""
Tests for zotero-index.py, against the synthetic export in tests/export.json.

Most tests drive the real CLI in a subprocess, with XDG_CONFIG_HOME and XDG_CACHE_HOME pointed into a temp dir; path mapping is also tested directly.

    uv run tests/test_zotero_index.py   # self-contained (installs pytest via uv)
    pytest tests/                       # if pytest is already available
"""
import importlib.util, json, os, shutil, subprocess, sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "zotero-index.py"
spec = importlib.util.spec_from_file_location("zotero_index", SCRIPT)
zi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zi)

CONFIG = """export = "{export}"
[path_map]
'C:\\Users\\alice\\Sync\\Zotero' = "/home/alice/sync/Zotero"
'C:\\Users\\alice\\Sync\\Zotero\\Archive' = "/mnt/archive"
"""


@pytest.fixture
def env(tmp_path):
    export = tmp_path / "library.json"
    shutil.copy(HERE / "export.json", export)
    (tmp_path / "config" / "zotero-index").mkdir(parents=True)
    (tmp_path / "config" / "zotero-index" / "config.toml").write_text(CONFIG.format(export=export))
    return tmp_path


def run(env, *args, cmd=(sys.executable, str(SCRIPT))):
    e = dict(os.environ, XDG_CONFIG_HOME=str(env / "config"), XDG_CACHE_HOME=str(env / "cache"), HOME=str(env))
    return subprocess.run([*cmd, *args], capture_output=True, text=True, env=e, cwd=env)


def touch_later(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


# --- path mapping ---

PATH_MAP = {r"C:\Users\alice\Sync\Zotero": "~/sync/Zotero", r"C:\Users\alice\Sync\Zotero\Archive": "/mnt/archive", "/srv/zotero": "/data/zotero"}


@pytest.mark.parametrize("raw, expected", [
    (r"C:\Users\alice\Sync\Zotero\sm\smith_example_2020.pdf", (os.path.expanduser("~/sync/Zotero/sm/smith_example_2020.pdf"), True)),
    (r"C:\Users\alice\Sync\Zotero\Archive\x.pdf", ("/mnt/archive/x.pdf", True)),           # longest prefix wins
    (r"c:\users\ALICE\sync\zotero\sm\a.pdf", (os.path.expanduser("~/sync/Zotero/sm/a.pdf"), True)),  # Windows prefixes ignore case
    (r"C:\Users\alice\Sync\ZoteroOld\a.pdf", ("C:/Users/alice/Sync/ZoteroOld/a.pdf", False)),  # only on a whole component
    (r"C:\Users\alice\Zotero\storage\K\a.pdf", ("C:/Users/alice/Zotero/storage/K/a.pdf", False)),
    ("/srv/zotero/a\\b.pdf", ("/data/zotero/a\\b.pdf", True)),                                # POSIX: backslash is just a character
    ("/SRV/zotero/a.pdf", ("/SRV/zotero/a.pdf", False)),                                      # POSIX prefixes are case-sensitive
])
def test_map_path(raw, expected):
    assert zi.map_path(raw, PATH_MAP) == expected


def test_empty_path_map_passes_paths_through():
    assert zi.map_path(r"C:\x\y.pdf", {}) == ("C:/x/y.pdf", True)


# --- config ---

def test_missing_config_names_expected_path(env):
    shutil.rmtree(env / "config")
    r = run(env, "path", "smith_example_2020")
    assert r.returncode == 3 and str(env / "config" / "zotero-index" / "config.toml") in r.stderr


def test_missing_export_is_a_short_error(env):
    (env / "library.json").unlink()
    r = run(env, "show", "smith_example_2020")
    assert r.returncode == 3 and "export not found" in r.stderr and "Traceback" not in r.stderr


def test_export_flag_overrides_config_and_works_without_one(env):
    other = env / "other.json"
    data = json.loads((env / "library.json").read_text())
    data["items"][0]["title"] = "From the other export"
    other.write_text(json.dumps(data))
    assert "From the other export" in run(env, "show", "smith_example_2020", "--export", str(other)).stdout
    shutil.rmtree(env / "config")
    r = run(env, "path", "smith_example_2020", "--export", str(other))  # no config: no path_map, paths pass through
    assert r.returncode == 0 and "C:/Users/alice/Sync/Zotero/sm/smith_example_2020.pdf" in r.stdout


def test_config_flag_overrides_location(env):
    alt = env / "alt.toml"
    alt.write_text(f'export = "library.json"\n[path_map]\n\'C:\\Users\\alice\\Sync\\Zotero\' = "/elsewhere"\n')  # relative to the config
    shutil.rmtree(env / "config")
    r = run(env, "--config", str(alt), "path", "smith_example_2020", "--ext", "pdf")
    assert r.stdout == "/elsewhere/sm/smith_example_2020.pdf\n"


def test_explicit_missing_config_is_an_error_even_with_export(env):
    r = run(env, "show", "smith_example_2020", "--config", str(env / "nope.toml"), "--export", str(env / "library.json"))
    assert r.returncode == 3 and "nope.toml" in r.stderr


# --- index freshness ---

def test_rebuilds_when_export_changes(env):
    assert "An Example of Usable Security" in run(env, "show", "smith_example_2020").stdout
    data = json.loads((env / "library.json").read_text())
    data["items"][0]["title"] = "A Retitled Example"
    (env / "library.json").write_text(json.dumps(data))
    touch_later(env / "library.json")
    assert "A Retitled Example" in run(env, "show", "smith_example_2020").stdout


def test_rebuilds_when_config_changes(env):
    assert run(env, "path", "smith_example_2020", "--ext", "pdf").stdout.startswith("/home/alice/sync/Zotero/")
    cfg = env / "config" / "zotero-index" / "config.toml"
    cfg.write_text(cfg.read_text().replace("/home/alice/sync/Zotero", "/media/zotero"))
    touch_later(cfg)
    assert run(env, "path", "smith_example_2020", "--ext", "pdf").stdout == "/media/zotero/sm/smith_example_2020.pdf\n"


def test_fresh_index_does_not_read_the_export(env):
    assert run(env, "show", "smith_example_2020").returncode == 0
    (env / "library.json").chmod(0)  # still stat-able, no longer readable
    try:
        assert run(env, "show", "smith_example_2020").returncode == 0
    finally:
        (env / "library.json").chmod(0o644)
    assert not list((env / "cache" / "zotero-index").glob(".tmp-*"))


# --- commands ---

def test_path_and_ext_filter(env):
    r = run(env, "path", "smith_example_2020")
    assert r.stdout.splitlines() == ["/home/alice/sync/Zotero/sm/smith_example_2020.pdf", "/mnt/archive/smith_example_2020-slides.pptx"]
    assert run(env, "path", "smith_example_2020", "--ext", ".PDF").stdout.splitlines() == ["/home/alice/sync/Zotero/sm/smith_example_2020.pdf"]


def test_path_exit_1_when_none(env):
    assert run(env, "path", "lee_noattachment_2018").returncode == 1
    assert run(env, "path", "no_such_2000").returncode == 1
    assert run(env, "path", "smith_example_2020", "--ext", "epub").returncode == 1


def test_path_unmapped_goes_to_stderr(env):
    r = run(env, "path", "kim_storage_2022")
    assert r.returncode == 1 and r.stdout == "" and "unmapped: C:/Users/alice/Zotero/storage/ABCD1234/kim_storage_2022.pdf" in r.stderr
    j = json.loads(run(env, "path", "kim_storage_2022", "--json").stdout)
    assert j[0]["mapped"] is False


def test_duplicate_citekey_prints_all_and_exits_2(env):
    r = run(env, "path", "doe_duplicate_2021")
    assert r.returncode == 2 and len(r.stdout.splitlines()) == 2 and "CCCC3333" in r.stderr and "DDDD4444" in r.stderr
    r = run(env, "show", "doe_duplicate_2021", "--json")
    assert r.returncode == 2 and {x["itemKey"] for x in json.loads(r.stdout)} == {"CCCC3333", "DDDD4444"}


def test_show_record(env):
    r = run(env, "show", "smith_example_2020", "--json")
    (rec,) = json.loads(r.stdout)
    assert rec["authors"] == ["Smith, Alice", "Jones, Carol"]  # editors are not authors
    assert (rec["year"], rec["doi"], rec["tags"]) == ("2020", "10.1234/example.2020", ["usability", "Security"])
    assert len(rec["attachments"]) == 2  # the URL-only attachment has no path


def test_collection_paths(env):
    (rec,) = json.loads(run(env, "show", "smith_example_2020", "--json").stdout)
    assert rec["collections"] == ["Reading", "Teaching", "Teaching/Module A/Week 1"]
    (rec,) = json.loads(run(env, "show", "org_handbook_2019", "--json").stdout)
    assert rec["collections"] == ["Teaching/Module A"]


def test_doi_lookup(env):
    assert run(env, "doi", "10.1234/EXAMPLE.2020").stdout == "smith_example_2020\n"
    assert run(env, "doi", "https://doi.org/10.5555/handbook").stdout == "org_handbook_2019\n"  # DOI from the extra field
    assert run(env, "doi", "10.0000/none").returncode == 1


def test_search(env):
    assert run(env, "search", "EXAMPLE", "smith").stdout == "smith_example_2020  2020  Smith, Alice  An Example of Usable Security\n"
    assert run(env, "search", "organisation").stdout.startswith("org_handbook_2019  2019  Example Organisation")
    assert {l.split()[0] for l in run(env, "search", "--tag", "USABILITY").stdout.splitlines()} == {"smith_example_2020", "lee_noattachment_2018"}
    assert {l.split()[0] for l in run(env, "search", "--collection", "module a").stdout.splitlines()} == {"smith_example_2020", "org_handbook_2019"}
    assert {l.split()[0] for l in run(env, "search", "--collection", "Teaching/Module A/Week 1").stdout.splitlines()} == {"smith_example_2020"}
    assert run(env, "search", "--collection", "Module").returncode == 1  # whole components only
    assert {l.split()[0] for l in run(env, "search", "--year", "2019-2020").stdout.splitlines()} == {"smith_example_2020", "org_handbook_2019"}
    assert run(env, "search", "--year", "2022").stdout.startswith("kim_storage_2022  2022")
    assert run(env, "search", "nothing matches this").returncode == 1


def test_check(env):
    r = run(env, "check")
    assert r.returncode == 0
    out = r.stdout
    assert "6 items" in out
    assert "## 1 citekeys shared by several items\ndoe_duplicate_2021\n  CCCC3333  A Duplicated Paper\n  DDDD4444" in out
    assert "## 1 attachment filenames not starting with the citekey" in out and "org_handbook_2019  /home/alice/sync/Zotero/or/scan0001.pdf" in out
    assert "## 1 items without attachments\nlee_noattachment_2018" in out
    assert "## 1 unmapped attachment paths\nkim_storage_2022  C:/Users/alice/Zotero/storage" in out
    j = json.loads(run(env, "check", "--json").stdout)
    assert [d["citekey"] for d in j["duplicate_citekeys"]] == ["doe_duplicate_2021"]
    assert [m["citekey"] for m in j["misnamed_attachments"]] == ["org_handbook_2019"]


def test_runs_through_a_symlink(env):
    link = env / "bin" / "zotero-index"
    link.parent.mkdir()
    link.symlink_to(SCRIPT)
    r = run(env, "doi", "10.1234/example.2020", cmd=(sys.executable, str(link)))
    assert r.stdout == "smith_example_2020\n"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
