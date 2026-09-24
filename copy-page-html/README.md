# copy-page-html

A userscript that copies the HTML of one element on a page to the clipboard — no devtools, no "inspect element", no right-click "copy outer HTML".
Point it at a page once, click the element you want (e.g. a Drupal site's `<main>`), and it's copied.
The CSS selector you picked is cached per site, so on later visits to the same site a single menu click re-copies it — handy for pages you can't otherwise automate, such as ones sitting behind Cloudflare.

## Install

1. Install [Violentmonkey](https://violentmonkey.github.io/) or [Tampermonkey](https://www.tampermonkey.net/) (or another compatible userscript manager — see [Compatibility](#compatibility)).
2. Create a new script and paste the contents of [`copy-page-html.user.js`](copy-page-html.user.js), or open the raw file and let the manager offer to install it.
3. Open any page.

## Use

Open the userscript manager's extension-icon menu — this is the "press a button in the menu" the script is built around, not a page context menu:

- **Pick element & copy HTML…** — enters picker mode. Hover highlights whatever element is under the cursor with an orange outline and shows its generated selector in a tooltip; click it to copy its `outerHTML` to the clipboard. **Shift-click copies `innerHTML` instead** (the element's contents without its own opening/closing tag). Esc cancels.
- **Copy cached HTML (…)** — appears once you've picked something on the current site. Re-runs `document.querySelector()` with the cached selector and copies again immediately, no picking needed. This is the one-click path for repeat visits.
- **Clear cached selector for this site** — forget the cached selector so **Pick element…** starts fresh.

The cache key is the page's **hostname**, and it stores one selector (plus outer/inner mode) per host — picking again overwrites it.

## How the selector is built

`cssPath()` walks up from the clicked element, at each level preferring `#id`, else `tag` or `tag:nth-of-type(n)` among same-tag siblings, and stops as soon as the accumulated selector uniquely matches one element (or it reaches `<html>`).
For a landmark like `<main>` that's usually the whole selector — see the bundled fixture:

```
main.main-content.main-content--with-left-navigation.col-lg-9
```

only needs `main` once it turns out to be the only one on the page.

## Compatibility

Built and tested against Violentmonkey; it should work unmodified in Tampermonkey too, since it only uses APIs both implement: `GM_setClipboard`, `GM_registerMenuCommand` / `GM_unregisterMenuCommand`, and `GM_setValue` / `GM_getValue` / `GM_deleteValue`.
`GM_setClipboard` is not part of stock Greasemonkey, so plain Greasemonkey isn't supported.

`GM_setClipboard` (rather than `navigator.clipboard.writeText`) is deliberate: it's a privileged call the extension makes on the script's behalf, so it works from a menu-command click even when the page itself doesn't currently have focus — the standard Clipboard API would silently fail there.

## Limitations

- **Same-origin iframes only reach the top document.** The script only runs in the top-level page (`window.top === window`), so an element that only exists inside an `<iframe>` can't be picked.
- **`file://` pages need the manager's "allow access to file URLs" permission** enabled for the fixture below to work; testing over `http://localhost` avoids that entirely.

## Testing with the bundled fixture

[`ucl-ethics.local.html`](ucl-ethics.local.html) is a saved copy of a real Drupal page (UCL's ethics site, which sits behind Cloudflare and can't easily be curl'd), kept here to exercise the picker against realistic markup.
Serve it locally rather than opening it as `file://` (see [Limitations](#limitations)):

```bash
python -m http.server -d copy-page-html 8000
```

then open `http://localhost:8000/ucl-ethics.local.html`, pick **main**, and confirm the clipboard holds its full `outerHTML`.
