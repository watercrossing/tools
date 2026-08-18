#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest"]
# ///
"""
Tests for repo-web-view.py.

Each test drives the real CLI end-to-end via `uv run repo-web-view.py`, so the tool's
own inline dependencies are exercised exactly as a user would run them.

    uv run tests/test_repo_web_view.py       # self-contained (installs pytest via uv)
    pytest tests/                            # if pytest is already available
"""
import json, socket, subprocess, sys, time, urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "repo-web-view.py"


@pytest.fixture
def sample_repo(tmp_path):
    src = tmp_path / "repo"
    (src / "toolA").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "README.md").write_text("# Sample\n\nWelcome to **sample**. See [toolA](toolA/) and [notes](notes.txt).\n\n"
                                   "Also [docs](docs.md#bit), [nested](toolA/README.md), [gone](missing.md) and "
                                   "[remote](https://example.com/x.md).\n", encoding="utf-8")
    (src / "docs.md").write_text("# Docs\n\nA standalone page, not a README.\n\n"
                                 "## Deep\n\n" + "filler " * 60 + "needle-far-down\n", encoding="utf-8")
    (src / "notes.txt").write_text("plain file\n", encoding="utf-8")
    (src / "hook.php").write_text('<?php echo "SHOULD NOT RUN"; ?>\n', encoding="utf-8")
    (src / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (src / "toolA" / "README.md").write_text("# toolA\n\n```python\nprint('hi')\n```\n", encoding="utf-8")
    (src / "toolA" / "toolA.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "toolA" / "toolA.html").write_text("<!doctype html><h1>toolA runs</h1>", encoding="utf-8")
    return src


def build(src, out, *extra):
    return subprocess.run(["uv", "run", str(SCRIPT), str(src), str(out), "--force", *extra],
                          capture_output=True, text=True)


def footer_of(page):
    return page.split("<footer>", 1)[1].split("</footer>", 1)[0]


def listing_of(page):
    return page.split('class="listing"', 1)[1].split("</section>", 1)[0]


def search_index(out):
    raw = (out / "search-index.js").read_text(encoding="utf-8")
    assert raw.startswith("window.__rwvSearch = ")
    return json.loads(raw[len("window.__rwvSearch = "):].rstrip().removesuffix(";"))


def test_generates_index_per_dir(sample_repo, tmp_path):
    out = tmp_path / "site"
    assert build(sample_repo, out).returncode == 0
    assert (out / "index.html").is_file()
    assert (out / "toolA" / "index.html").is_file()


def test_root_renders_readme_and_lists(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "<h1" in page and "Sample" in page      # README rendered above the listing
    assert 'href="toolA/"' in page                 # subdir link keeps trailing slash -> renders
    assert 'href="notes.txt"' in page              # file link (no slash) -> downloads


def test_dotfiles_excluded(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    assert not (out / ".git").exists()
    assert ".git" not in (out / "index.html").read_text(encoding="utf-8")


def test_extra_exclude_glob(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--exclude", "*.txt")
    assert not (out / "notes.txt").exists()


def test_code_block_highlighted(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    assert 'class="hl' in (out / "toolA" / "index.html").read_text(encoding="utf-8")


def test_footer_note_stamped_on_every_page(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--footer-note", "a95daf6", "--footer-note-url", "https://example.com/commit/a95daf6")
    for page in (out / "index.html", out / "toolA" / "index.html"):
        footer = footer_of(page.read_text(encoding="utf-8"))
        assert '<a href="https://example.com/commit/a95daf6">a95daf6</a>' in footer


def test_footer_note_without_url_is_plain_text(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--footer-note", "a95daf6")
    footer = footer_of((out / "index.html").read_text(encoding="utf-8"))
    assert "a95daf6" in footer and "<a " not in footer


def test_footer_note_escaped(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--footer-note", "<script>x</script>", "--footer-note-url", 'https://e.com/"onx')
    footer = footer_of((out / "index.html").read_text(encoding="utf-8"))
    assert "<script>" not in footer and "&lt;script&gt;" in footer
    assert 'href="https://e.com/&quot;onx"' in footer


def test_footer_note_absent_by_default(sample_repo, tmp_path):
    # An empty note is how the deploy script says "no commit to stamp", so it must not leave a stray separator.
    out = tmp_path / "site"
    build(sample_repo, out, "--footer-note", "", "--footer-note-url", "")
    assert "<footer>Generated by repo-web-view</footer>" in (out / "index.html").read_text(encoding="utf-8")


def test_htaccess_written(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    ht = (out / ".htaccess").read_text(encoding="utf-8")
    assert "attachment" in ht and "inline" in ht      # files download, .html renders
    assert "html" in ht.lower()
    assert "RemoveHandler" in ht and "SetHandler default-handler" in ht   # scripts don't execute


def test_no_htaccess_flag(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--no-htaccess")
    assert not (out / ".htaccess").exists()


def test_nested_output_rejected(sample_repo):
    r = build(sample_repo, sample_repo / "site")
    assert r.returncode != 0
    assert "nested" in (r.stderr + r.stdout).lower()


def test_markdown_downloads_by_default(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    assert not (out / "docs.md.html").exists()
    page = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="docs.md"' in page and "docs.md.html" not in page


def test_render_markdown_makes_a_page_per_md(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    for page in (out / "docs.md.html", out / "README.md.html", out / "toolA" / "README.md.html"):
        assert page.is_file()
    assert (out / "docs.md").is_file()                                  # the source file is still published
    assert "A standalone page" in (out / "docs.md.html").read_text(encoding="utf-8")


def test_render_markdown_listing_links_to_the_page(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    listing = listing_of((out / "index.html").read_text(encoding="utf-8"))
    assert '<a href="docs.md.html">docs.md</a>' in listing              # linked to the page, labelled as the file
    assert '<a href="notes.txt">notes.txt</a>' in listing               # non-markdown still downloads
    assert "docs.md.html</a>" not in listing                            # and the page is not listed as a file of its own


def test_render_markdown_page_has_breadcrumb_and_listing(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    page = (out / "toolA" / "README.md.html").read_text(encoding="utf-8")
    assert '<a href="../">repo</a>' in page and '<a href="./">toolA</a>' in page
    assert '<span class="here">README.md</span>' in page
    listing = listing_of(page)
    assert '<a href="toolA.py">toolA.py</a>' in listing                 # same listing as the folder's index


def test_render_markdown_rewrites_md_links(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    body = (out / "index.html").read_text(encoding="utf-8").split('class="listing"')[0]
    assert 'href="docs.md.html#bit"' in body                            # fragment kept
    assert 'href="toolA/README.md.html"' in body                        # relative path into a subfolder
    assert 'href="missing.md"' in body                                  # no such file -> left alone
    assert 'href="https://example.com/x.md"' in body                    # remote -> left alone


def test_render_markdown_footer_note_on_md_pages(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown", "--footer-note", "a95daf6")
    assert "a95daf6" in footer_of((out / "docs.md.html").read_text(encoding="utf-8"))


def _free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait(url, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"server did not start: {url}")


def test_serve_forces_file_download_but_renders_folder(sample_repo, tmp_path):
    out = tmp_path / "site"
    port = _free_port()
    proc = subprocess.Popen(["uv", "run", str(SCRIPT), str(sample_repo), str(out),
                             "--force", "--serve", str(port)])
    try:
        base = f"http://localhost:{port}"
        _wait(base + "/")
        with urllib.request.urlopen(base + "/notes.txt") as r:          # a plain file downloads
            assert "attachment" in (r.headers.get("Content-Disposition") or "")
        with urllib.request.urlopen(base + "/hook.php") as r:           # a script downloads raw, un-executed
            assert "attachment" in (r.headers.get("Content-Disposition") or "")
            assert "<?php" in r.read().decode()
        with urllib.request.urlopen(base + "/docs.md") as r:            # markdown downloads without the flag
            assert "attachment" in (r.headers.get("Content-Disposition") or "")
        with urllib.request.urlopen(base + "/toolA/toolA.html") as r:   # an .html tool renders inline
            assert "attachment" not in (r.headers.get("Content-Disposition") or "")
            assert "toolA runs" in r.read().decode()
        with urllib.request.urlopen(base + "/") as r:                   # a folder renders inline
            body = r.read().decode()
            assert "attachment" not in (r.headers.get("Content-Disposition") or "")
        assert "<h1" in body
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_serve_renders_markdown_pages(sample_repo, tmp_path):
    out = tmp_path / "site"
    port = _free_port()
    proc = subprocess.Popen(["uv", "run", str(SCRIPT), str(sample_repo), str(out),
                             "--force", "--render-markdown", "--serve", str(port)])
    try:
        base = f"http://localhost:{port}"
        _wait(base + "/")
        with urllib.request.urlopen(base + "/docs.md.html") as r:       # the generated page renders inline
            assert "attachment" not in (r.headers.get("Content-Disposition") or "")
            assert "A standalone page" in r.read().decode()
        with urllib.request.urlopen(base + "/docs.md") as r:            # the source file still downloads
            assert "attachment" in (r.headers.get("Content-Disposition") or "")
    finally:
        proc.terminate()
        proc.wait(timeout=15)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_search_is_on_by_default(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    assert (out / "search-index.js").is_file()
    for page in (out / "index.html", out / "toolA" / "index.html"):
        text = page.read_text(encoding="utf-8")
        assert 'class="search-open"' in text and 'class="search-modal"' in text


def test_search_root_is_relative_to_the_page(sample_repo, tmp_path):
    # One index at the site root serves every page, so each page needs its own way back to it.
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    assert 'const ROOT = "./";' in (out / "index.html").read_text(encoding="utf-8")
    assert 'const ROOT = "../";' in (out / "toolA" / "index.html").read_text(encoding="utf-8")
    assert 'const ROOT = "../";' in (out / "toolA" / "README.md.html").read_text(encoding="utf-8")


def test_no_search_flag(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--no-search")
    assert not (out / "search-index.js").exists()
    text = (out / "index.html").read_text(encoding="utf-8")
    assert 'class="search-open"' not in text and '<div class="search-modal"' not in text
    assert "search-index.js" not in text and "<script" not in text


def test_search_indexes_readme_sections(sample_repo, tmp_path):
    # A record per heading, located at the page that renders it plus the heading's own anchor.
    out = tmp_path / "site"
    build(sample_repo, out)
    locs = {rec[0]: rec for rec in search_index(out)["pages"]}
    assert "#sample" in locs and locs["#sample"][1] == "repo"          # the root README, at the root page
    assert locs["#sample"][2] == "Sample"                             # section = the heading it sits under
    assert "Welcome to sample" in locs["#sample"][3]                  # text, with the markup stripped
    assert locs["toolA/#toola"][1] == "repo / toolA"                  # a subfolder's README, at that folder


def test_search_skips_markdown_that_does_not_render(sample_repo, tmp_path):
    # Without --render-markdown a plain .md is a download, with no page for a result to point at.
    out = tmp_path / "site"
    build(sample_repo, out)
    assert not any(rec[0].startswith("docs.md") for rec in search_index(out)["pages"])


def test_search_indexes_rendered_markdown_pages(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    pages = search_index(out)["pages"]
    assert any(rec[0] == "docs.md.html#docs" for rec in pages)
    # A README is already indexed at its folder's page; its own generated page must not double it up.
    assert not any(rec[0].startswith("README.md.html") for rec in pages)


def test_search_indexes_whole_sections_not_just_the_snippet(sample_repo, tmp_path):
    # The page shows the first ~200 characters of a match, but all of it has to be searchable.
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    deep = next(rec for rec in search_index(out)["pages"] if rec[0] == "docs.md.html#deep")
    assert "needle-far-down" in deep[3] and len(deep[3]) > 400


def test_search_lists_files_and_folders(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out)
    files = {rec[1]: rec for rec in search_index(out)["files"]}
    assert files["toolA/"][0] == "toolA/" and files["toolA/"][2] == ""          # a folder, no size
    assert files["notes.txt"][0] == "notes.txt" and files["notes.txt"][2]       # a file, with its size
    assert files["toolA/toolA.py"][0] == "toolA/toolA.py"                       # nested, path relative to the root
    assert ".git/config" not in files                                           # dotfiles never make it into the tree


def test_search_files_link_to_rendered_markdown(sample_repo, tmp_path):
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    files = {rec[1]: rec for rec in search_index(out)["files"]}
    assert files["docs.md"][0] == "docs.md.html"        # labelled as the file, pointing at its page


def test_search_index_is_not_listed_as_a_file(sample_repo, tmp_path):
    # It is part of the site's machinery, like index.html, not part of the tree being published.
    out = tmp_path / "site"
    build(sample_repo, out)
    listing = listing_of((out / "index.html").read_text(encoding="utf-8"))
    assert "search-index.js" not in listing
    assert not any(rec[1] == "search-index.js" for rec in search_index(out)["files"])


def test_search_index_escapes_nothing_into_html(sample_repo, tmp_path):
    # The index is data for the page's JS; markup in a README must arrive as text, not as tags.
    (sample_repo / "evil.md").write_text("# Evil\n\nA <script>alert(1)</script> and <b>bold</b>.\n", encoding="utf-8")
    out = tmp_path / "site"
    build(sample_repo, out, "--render-markdown")
    evil = next(rec for rec in search_index(out)["pages"] if rec[0].startswith("evil.md.html"))
    assert "<script>" not in evil[3] and "<b>" not in evil[3]
    assert "alert(1)" in evil[3] and "bold" in evil[3]
