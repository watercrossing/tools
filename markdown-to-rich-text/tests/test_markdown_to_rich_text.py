#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pytest", "playwright"]
# ///
"""
Browser tests for markdown-to-rich-text.html: drive a real Chromium, then read back what landed on the clipboard.

    uv run tests/test_markdown_to_rich_text.py            # self-contained
    uv run --with playwright playwright install chromium  # once, to get the browser

The page loads marked and DOMPurify from jsdelivr, so these need network access.
"""
import sys
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PAGE = (Path(__file__).resolve().parent.parent / "markdown-to-rich-text.html").as_uri()

SAMPLE = """# Title

## Section

Some **bold**, *italic* and `inline code`, a [web link](https://example.com) and a [repo link](src/app.ts#L42).

- [x] done
- [ ] to do

| Name | Value |
| --- | :---: |
| a | 1 |

```python
print("hi")
```

> quoted

<img src=x onerror="window.pwned = 1">
"""

READ_CLIPBOARD = """async () => {
  const [item] = await navigator.clipboard.read();
  const get = async t => item.types.includes(t) ? (await item.getType(t)).text() : null;
  return { html: await get("text/html"), text: await get("text/plain") };
}"""


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium not installed: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page()
    pg.goto(PAGE)  # module scripts, CDN imports included, run before the load event goto waits for
    yield pg
    ctx.close()


def copied(page, markdown):
    page.fill("#md", markdown)
    page.click("#copy")
    page.wait_for_selector("#status:has-text('Copied')")
    return page.evaluate(READ_CLIPBOARD)


def test_copy_puts_formatted_html_and_markdown_on_the_clipboard(page):
    clip = copied(page, SAMPLE)
    html = clip["html"]
    assert clip["text"].replace("\r\n", "\n") == SAMPLE
    assert "<h1>Title</h1>" in html and "<h2>Section</h2>" in html  # unstyled, so they map onto the destination's Heading styles
    assert "<strong>bold</strong>" in html and "<em>italic</em>" in html
    assert 'href="https://example.com/"' in html  # Chromium normalises URLs when reading the clipboard back


def test_outlook_friendly_rewrites(page):
    html = copied(page, SAMPLE)["html"]
    assert "src/app.ts" not in html and "repo link" in html      # relative links keep only their text
    assert "<img" not in html                                     # and relative images, unloadable in an email, go
    assert "☑" in html and "☐" in html and "<input" not in html  # task lists become glyphs
    assert 'style="border: 1px solid' in html                    # table cells carry their own borders
    assert "Consolas" in html                                     # code is monospaced inline, not via a stylesheet
    assert "border-left: 3px solid" in html                       # blockquote bar


def test_raw_html_is_sanitised(page):
    html = copied(page, SAMPLE)["html"]
    assert "onerror" not in html
    assert page.evaluate("window.pwned") is None


def test_paste_replaces_text_and_copies_straight_away(page):
    page.fill("#md", "old text that should go")
    page.evaluate("t => navigator.clipboard.writeText(t)", "# Pasted\n\n- one\n- two\n")
    page.focus("#md")
    page.keyboard.press("Control+V")
    page.wait_for_selector("#status:has-text('Copied')")
    assert page.input_value("#md") == "# Pasted\n\n- one\n- two\n"
    html = page.evaluate(READ_CLIPBOARD)["html"]
    assert "<h1>Pasted</h1>" in html and ">one</li>" in html


def test_paste_edits_normally_when_auto_copy_is_off(page):
    page.uncheck("#auto")
    page.fill("#md", "start ")
    page.evaluate("t => navigator.clipboard.writeText(t)", "**more**")
    page.focus("#md")
    page.keyboard.press("End")
    page.keyboard.press("Control+V")
    page.wait_for_function("document.querySelector('#md').value.includes('more')")
    assert page.input_value("#md") == "start **more**"
    assert page.inner_html("#preview").strip() == '<p style="font-size: 11pt;">start <strong>more</strong></p>'


def test_single_newlines_are_line_breaks(page):
    html = copied(page, "**Traveller:** me\n**Purpose:** a workshop\n")["html"]
    assert "<strong>Traveller:</strong> me<br><strong>Purpose:</strong> a workshop" in html


def test_text_size_applies_to_body_text_but_not_headings(page):
    html = copied(page, "# Head\n\nbody\n\n- item\n")["html"]
    assert "<h1>Head</h1>" in html
    assert '<p style="font-size: 11pt;">body</p>' in html and '<li style="font-size: 11pt;">item</li>' in html
    page.select_option("#size", "")                               # leave it to the destination
    html = copied(page, "# Head\n\nbody\n")["html"]
    assert "<p>body</p>" in html
    page.reload()
    assert page.input_value("#size") == ""                        # the choice is remembered


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
