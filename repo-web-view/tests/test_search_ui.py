#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest", "playwright"]
# ///
"""
Browser tests for the search box repo-web-view puts on every generated page.

These drive a real Chromium, so they are kept apart from test_repo_web_view.py, which needs
nothing but pytest. They skip themselves if Playwright's browser has not been installed.

    uv run tests/test_search_ui.py                        # self-contained
    uv run --with playwright playwright install chromium  # once, to get the browser

The site is served over HTTP rather than opened off disk on purpose: a folder link ("tool/")
only resolves to index.html through a server, exactly as in production.
"""
import socket, subprocess, sys, time, urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

SCRIPT = Path(__file__).resolve().parent.parent / "repo-web-view.py"


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


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    # A repo with enough text that a query has to reach past the first paragraph of a section.
    tmp = tmp_path_factory.mktemp("search-ui")
    src = tmp / "repo"
    (src / "toolA").mkdir(parents=True)
    (src / "README.md").write_text("# Sample\n\nTop level readme.\n\n## Overview\n\nA wombat lives here. "
                                   + "padding words. " * 200
                                   + "\n\n## Deployment\n\nAnother wombat. " + "padding words. " * 30
                                   + "\n\nThe marker word is quokka.\n", encoding="utf-8")
    (src / "docs.md").write_text("# Docs\n\nA standalone page.\n\n## Details\n\nMentions quokka too.\n", encoding="utf-8")
    (src / "toolA" / "README.md").write_text("# toolA\n\nWhat toolA does.\n", encoding="utf-8")
    (src / "toolA" / "toolA.py").write_text("print('hi')\n", encoding="utf-8")
    out, port = tmp / "site", _free_port()
    proc = subprocess.Popen(["uv", "run", str(SCRIPT), str(src), str(out), "--force",
                             "--render-markdown", "--serve", str(port)])
    base = f"http://localhost:{port}"
    try:
        _wait(base + "/")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@pytest.fixture
def page(site):
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:                       # browser not installed: nothing to test against
            pytest.skip(f"chromium unavailable: {exc}")
        pg = browser.new_page(viewport={"width": 1200, "height": 800})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(site + "/")
        yield pg
        assert pg.errors == []
        browser.close()


def search(pg, query):
    pg.keyboard.press("/")
    pg.wait_for_selector(".search-modal:not([hidden])")
    pg.fill(".search-input", query)
    pg.wait_for_timeout(150)


def test_modal_starts_hidden_and_opens_on_slash(page):
    assert page.locator(".search-modal").is_hidden()
    page.keyboard.press("/")
    page.wait_for_selector(".search-modal:not([hidden])")
    assert page.evaluate("document.activeElement.classList.contains('search-input')")


def test_opens_with_ctrl_k_and_closes_on_escape(page):
    page.keyboard.press("Control+k")
    page.wait_for_selector(".search-modal:not([hidden])")
    page.keyboard.press("Escape")
    assert page.locator(".search-modal").is_hidden()


def test_slash_typed_into_the_box_is_not_a_shortcut(page):
    search(page, "a/b")
    page.keyboard.press("/")
    assert page.input_value(".search-input") == "a/b/"


def test_results_are_grouped_with_pages_before_files(page):
    search(page, "toolA")
    assert page.locator(".search-group").all_text_contents() == ["Pages", "Files"]
    assert "matching result" in page.text_content(".search-status")


def test_a_hit_deep_links_into_the_section_it_matched(page):
    # "quokka" sits well past the start of the Deployment section, so this also pins down that the
    # whole section is searched and not just the part a result shows.
    search(page, "quokka")
    hit = page.locator(".hit").first
    assert hit.get_attribute("href").endswith("#deployment")
    with page.expect_navigation():
        hit.click()
    page.wait_for_timeout(200)
    assert page.evaluate("window.scrollY") > 0          # the browser landed on the heading, not the top


def test_search_works_from_a_nested_page(page, site):
    page.goto(site + "/toolA/")
    search(page, "docs")
    assert page.locator(".hit").first.get_attribute("href").startswith("../")


def test_file_hits_link_to_the_rendered_page_for_markdown(page):
    search(page, "docs.md")
    files = page.locator(".hit-file")
    assert files.count() >= 1
    assert files.first.get_attribute("href").endswith("docs.md.html")


def test_matches_are_highlighted(page):
    search(page, "quokka")
    assert page.locator(".hit mark").count() >= 1


def test_no_results_says_so(page):
    search(page, "zzzznotinthere")
    assert page.text_content(".search-status") == "No results"
    assert page.locator(".hit").count() == 0


def test_backdrop_click_closes(page):
    search(page, "toolA")
    page.mouse.click(600, 780)
    assert page.locator(".search-modal").is_hidden()


def test_sections_of_one_page_collapse_into_a_single_result(page):
    # "wombat" matches two sections of the root README; a long page must not fill the list with itself.
    search(page, "wombat")
    assert page.locator(".search-results > .hit").count() == 1
    assert page.locator(".hit-more").text_content() == "1 more on this page"
    assert page.locator(".hit-extra").is_hidden()


def test_folded_sections_stay_out_of_the_keyboard_walk(page):
    search(page, "wombat")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")                    # only one hit is showing, so this wraps back to it
    assert page.locator(".hit.active").count() == 1
    assert page.locator(".hit.active").first.get_attribute("href").endswith("#overview")


def test_more_on_this_page_expands_the_rest(page):
    search(page, "wombat")
    page.locator(".hit-more").click()
    page.wait_for_timeout(100)
    assert page.locator(".hit-extra .hit").first.is_visible()
    assert page.locator(".hit-more").count() == 0        # the link is spent once its sections are showing
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")                     # the revealed section is now part of the walk
    assert page.locator(".hit.active").first.get_attribute("href").endswith("#deployment")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
