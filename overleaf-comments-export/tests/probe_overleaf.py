#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["playwright"]
# ///
"""Live-site probe for overleaf-comments-export.user.js using Playwright.

Loads the userscript into a real Overleaf editor IN PAGE CONTEXT (like `@grant none`), then checks
the two things the UI has to get right — the **Comment sync** menu renders, and its **submenu opens
on click** — and dumps the DOM/CM6 internals the script leans on (the CM6 ranges state field, the
`/threads` payload, the "Help" nav anchor, track-changes and file-tree selectors) so we can keep the
fragile selectors honest against the live app.

This hits the network and drives a real (anonymous, link-shared) Overleaf project. It is NOT a
hermetic unit test — named `probe_*` (not `test_*`) so pytest never collects it.

One-time setup (downloads the browser binary; not a pip dep):
    uv run --with playwright playwright install chromium

Run:
    uv run overleaf-comments-export/tests/probe_overleaf.py [--headed] [--browser chromium|firefox]
                                                            [--url URL] [--shot-dir DIR]
"""
import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
USERSCRIPT = HERE.parent / "overleaf-comments-export.user.js"
# The read-&-edit share link for the throwaway test project. It resolves to /project/<id> — but only
# for a logged-in session (Overleaf attributes link-share edits to an account; it is NOT anonymous).
DEFAULT_URL = "https://www.overleaf.com/1938294149wxcwsfdhjbyq#5ab538"

# Auth material lives in a gitignored .auth/ dir and is NEVER printed or committed. Two ways in:
#   1. a session cookie (env OVERLEAF_SESSION, or .auth/overleaf_session.txt) — one copy-paste value;
#   2. a Playwright storage_state.json produced by `--login` (a one-time real, headed sign-in).
AUTH_DIR = HERE / ".auth"
STATE_FILE = AUTH_DIR / "overleaf-state.json"
COOKIE_FILE = AUTH_DIR / "overleaf_session.txt"
COOKIE_NAME = "overleaf_session2"


def load_cookie():
    """The Overleaf session cookie value from env or the gitignored file, or None. Treated as a secret."""
    val = os.environ.get("OVERLEAF_SESSION")
    if not val and COOKIE_FILE.exists():
        val = COOKIE_FILE.read_text(encoding="utf-8").strip()
    return val.strip() if val else None

# --- probe snippets (run in page context) --------------------------------------------------------

# Inspect the live DOM + CM6 state the userscript targets, WITHOUT the script loaded yet. Mirrors the
# real selectors: getRangesState (find a state.values field with ranges.comments+docId, never a fixed
# index), findHelpAnchor, trackChangesLikelyOn, texFileTreeItems.
PROBE_DOM = r"""
() => {
  const out = {};
  const content = document.querySelector('.cm-content');
  out.hasCmContent = !!content;
  const view = content && content.cmView && content.cmView.view;
  out.hasView = !!view;
  if (view && Array.isArray(view.state && view.state.values)) {
    const values = view.state.values;
    out.valuesCount = values.length;
    const idx = values.findIndex(v => v && v.ranges && Array.isArray(v.ranges.comments) && v.ranges.docId);
    out.rangesFieldIndex = idx;
    if (idx >= 0) {
      const rs = values[idx].ranges;
      out.docId = rs.docId;
      out.commentsCount = rs.comments.length;
      const c0 = rs.comments[0];
      out.sampleComment = c0 ? { keys: Object.keys(c0), op: c0.op } : null;
    }
  }
  const navRoot = document.querySelector('nav, header, .toolbar-header, .ol-cm-toolbar') || document;
  const els = [...navRoot.querySelectorAll('a, button, [role="menuitem"], [role="button"]')];
  out.navLabels = els.map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 50);
  const help = els.find(el => (el.textContent || '').trim().toLowerCase() === 'help');
  out.helpFound = !!help;
  if (help) {
    out.helpOuterHTML = help.outerHTML.slice(0, 400);
    const host = help.closest('li') || help.parentElement || help;
    out.helpHostTag = host.tagName;
    out.helpHostClass = host.className;
    out.helpParentClass = host.parentElement ? host.parentElement.className : null;
  }
  out.trackChangeMarkers = document.querySelectorAll('.ol-cm-change, ins.ol-cm-change, .track-changes-marker').length;
  const items = [...document.querySelectorAll('.file-tree [role="treeitem"], .file-tree li.entity')];
  out.fileTreeItems = items.length;
  out.texItems = items.filter(el => /\.tex\b/i.test(el.textContent || ''))
                      .map(el => (el.textContent || '').trim()).slice(0, 20);
  return out;
}
"""

# The credentialed same-origin /threads call, exactly as fetchThreads does it.
PROBE_THREADS = r"""
async () => {
  const pid = location.pathname.split('/')[2];
  if (!pid) return { ok: false, error: 'no /project/<id> in URL', href: location.href };
  try {
    const r = await fetch(`/project/${pid}/threads`, { credentials: 'include' });
    const ct = r.headers.get('content-type') || '';
    const text = await r.text();
    let json = null; try { json = JSON.parse(text); } catch (e) {}
    if (!json) return { ok: false, pid, status: r.status, contentType: ct, bodyHead: text.slice(0, 200) };
    const keys = Object.keys(json);
    const first = keys[0] ? json[keys[0]] : null;
    return {
      ok: true, pid, status: r.status, threadCount: keys.length, sampleThreadId: keys[0] || null,
      sampleResolved: first ? !!first.resolved : null,
      sampleMessages: first && first.messages
        ? first.messages.map(m => ({ content: m.content, user: m.user, timestamp: m.timestamp })) : null,
    };
  } catch (e) { return { ok: false, pid, error: String(e) }; }
}
"""


def dump(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="Overleaf project / share URL (default: the test project)")
    ap.add_argument("--browser", default="chromium", choices=["chromium", "firefox"], help="engine (default chromium)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--login", action="store_true",
                    help="one-time: open a real window, you sign in, then save the session to .auth/ and exit")
    ap.add_argument("--shot-dir", default=str(HERE / "test-results"), help="where to write screenshots (gitignored)")
    # Overleaf bounces the default headless UA (…HeadlessChrome…) to /restricted → /login, so present as
    # a normal desktop browser. Overridable if the UA string ever needs refreshing.
    ap.add_argument("--user-agent", default=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
                    help="User-Agent to present (default: a current desktop Chrome)")
    args = ap.parse_args()

    shot_dir = Path(args.shot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    script_src = USERSCRIPT.read_text(encoding="utf-8")
    results = {"passed": [], "failed": []}

    def check(name, ok):
        (results["passed"] if ok else results["failed"]).append(name)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    hide_webdriver = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    viewport = {"width": 1600, "height": 1000}

    with sync_playwright() as p:
        # --login: real headed sign-in, then persist the whole session to .auth/ and stop. Handles 2FA/
        # SSO/reCAPTCHA because a human drives it. Run this yourself; it needs a visible window.
        if args.login:
            AUTH_DIR.mkdir(parents=True, exist_ok=True)
            browser = getattr(p, args.browser).launch(headless=False)
            context = browser.new_context(viewport=viewport, user_agent=args.user_agent)
            context.add_init_script(hide_webdriver)
            page = context.new_page()
            page.goto("https://www.overleaf.com/login")
            print("Sign in in the window that opened, then open ANY project. Waiting up to 5 min …")
            try:
                page.wait_for_url(lambda u: "/project" in u, timeout=300000)
            except Exception:
                print("!! didn't reach a project in time — not saving. Re-run --login.")
                browser.close(); return 2
            context.storage_state(path=str(STATE_FILE))
            browser.close()
            print(f"Saved session to {STATE_FILE} (gitignored). Re-run without --login to probe.")
            return 0

        # Normal run: authenticate via saved storage_state if present, else inject the session cookie.
        storage = str(STATE_FILE) if STATE_FILE.exists() else None
        cookie = load_cookie()
        auth_mode = "storage-state" if storage else ("session-cookie" if cookie else "NONE — will hit /login")
        print(f"auth: {auth_mode}")
        browser = getattr(p, args.browser).launch(headless=not args.headed)
        context = browser.new_context(viewport=viewport, user_agent=args.user_agent, storage_state=storage)
        # Overleaf sniffs navigator.webdriver; hide it so we look like an ordinary tab (this is our own
        # link-shared test project, not an access control we're circumventing).
        context.add_init_script(hide_webdriver)
        if cookie and not storage:
            context.add_cookies([{"name": COOKIE_NAME, "value": cookie, "domain": "www.overleaf.com",
                                  "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"}])
        page = context.new_page()
        page.set_default_timeout(60000)

        print(f"Opening {args.url} …")
        page.goto(args.url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".cm-content", timeout=60000)
        except Exception as e:
            print(f"!! editor (.cm-content) never appeared: {e}")
            page.screenshot(path=str(shot_dir / "00-no-editor.png"), full_page=True)
            print(f"   screenshot: {shot_dir / '00-no-editor.png'}   final URL: {page.url}")
            if "/login" in page.url:
                print("   -> landed on /login: not authenticated. Provide a session first:\n"
                      "      • cookie:  put overleaf_session2 in .auth/overleaf_session.txt (or $OVERLEAF_SESSION), or\n"
                      "      • login:   uv run overleaf-comments-export/tests/probe_overleaf.py --login")
            browser.close()
            return 2
        # let the editor settle (ranges + nav render after the doc opens)
        page.wait_for_timeout(2500)
        print(f"Editor loaded. Final URL: {page.url}")
        page.screenshot(path=str(shot_dir / "01-editor.png"), full_page=False)

        dom = page.evaluate(PROBE_DOM)
        dump("DOM / CM6 probe (before injecting the userscript)", dom)
        threads = page.evaluate(PROBE_THREADS)
        dump("/threads probe", threads)

        # Inject the userscript in page context (bypasses CSP; matches @grant none). Wrap the file in an
        # arrow body so evaluate accepts it as a callable; the IIFE runs and wires up window.olcSync + menu.
        print("\nInjecting userscript …")
        page.evaluate("() => {\n" + script_src + "\n}")
        has_api = page.evaluate("() => !!(window.olcSync && window.olcSync.CORE)")
        check("window.olcSync exposed", has_api)

        # 1) menu renders
        menu = page.locator("#olc-comment-sync-menu")
        try:
            menu.wait_for(state="attached", timeout=8000)
        except Exception:
            pass
        check("Comment sync menu injected (#olc-comment-sync-menu present)", menu.count() > 0)
        sync_btn = page.get_by_role("button", name="Comment sync", exact=True)
        btn_visible = sync_btn.count() > 0 and sync_btn.first.is_visible()
        check("'Comment sync' button visible in the nav", btn_visible)
        if menu.count() > 0:
            page.screenshot(path=str(shot_dir / "02-menu-injected.png"), full_page=False)

        # 2) clicking opens the submenu (native dropdown: items are <a role="menuitem">, panel is a <ul>)
        item1 = page.get_by_role("menuitem", name="Sync comments to tex on this file", exact=True)
        item2 = page.get_by_role("menuitem", name="Send comments to tex on all files (experimental)", exact=True)
        check("submenu hidden before click", not item1.first.is_visible())
        if btn_visible:
            sync_btn.first.click()
            page.wait_for_timeout(300)
            opened = item1.first.is_visible() and item2.first.is_visible()
            check("submenu opens on click (both items visible)", opened)
            page.screenshot(path=str(shot_dir / "03-submenu-open.png"), full_page=False)

        print(f"\nScreenshots written to {shot_dir}")
        browser.close()

    print("\n================ SUMMARY ================")
    print(f"PASS: {len(results['passed'])}   FAIL: {len(results['failed'])}")
    for n in results["failed"]:
        print(f"  FAIL: {n}")
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
