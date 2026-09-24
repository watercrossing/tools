// ==UserScript==
// @name         Copy element HTML (pick & cache)
// @namespace    https://github.com/ingolfbecker/tools
// @version      0.1.0
// @description  Pick any element on a page and copy its outer/inner HTML to the clipboard from the userscript-manager menu, no devtools needed. The picked CSS selector is cached per site, so later visits are a single menu click.
// @author       Ingolf Becker
// @match        *://*/*
// @match        file:///*
// @grant        GM_setClipboard
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';

  // Only run in the top-level document — a page's iframes would otherwise each register their own
  // menu commands and picker, and elements inside a cross-origin iframe can't be reached anyway.
  if (window.top !== window) return;

  const LOG = '[copy-html]';
  const HIGHLIGHT_OUTLINE = '2px solid #ff5900';
  const cacheKey = () => `cph:${location.hostname}`;

  // ---- selector generation ----------------------------------------------------------------------

  function isUnique(selector) {
    try { return document.querySelectorAll(selector).length === 1; } catch { return false; }
  }

  // Build a short CSS selector that (re)identifies `el`: walk up from the element, at each level
  // preferring its own #id, else tag[:nth-of-type(n)] among same-tag siblings, joined with the child
  // combinator (each part really is the previous part's direct parent). Stops as soon as the
  // accumulated selector is globally unique, at an id (assumed unique enough on its own), or at <html>.
  function cssPath(el) {
    if (!(el instanceof Element)) return '';
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1) {
      let part;
      if (node.id) {
        part = `#${CSS.escape(node.id)}`;
      } else {
        part = node.tagName.toLowerCase();
        const parent = node.parentElement;
        if (parent) {
          const sameTag = Array.from(parent.children).filter(c => c.tagName === node.tagName);
          if (sameTag.length > 1) part += `:nth-of-type(${sameTag.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      const candidate = parts.join(' > ');
      if (node.id || node === document.documentElement || isUnique(candidate)) return candidate;
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  // ---- clipboard + cache --------------------------------------------------------------------------

  function copyAndReport(el, mode, selector, save) {
    const html = mode === 'inner' ? el.innerHTML : el.outerHTML;
    GM_setClipboard(html, 'text');
    if (save) {
      GM_setValue(cacheKey(), { selector, mode, savedAt: Date.now(), pageUrl: location.href });
      refreshMenu();
    }
    toast(`Copied ${selector} (${mode}, ${html.length.toLocaleString()} chars) to clipboard`);
    console.log(`${LOG} copied`, { selector, mode, chars: html.length });
  }

  function copyFromCache() {
    const cache = GM_getValue(cacheKey());
    if (!cache) { toast('No cached selection for this site yet — pick one first.', true); return; }
    const el = document.querySelector(cache.selector);
    if (!el) { toast(`Cached selector "${cache.selector}" not found on this page.`, true); return; }
    copyAndReport(el, cache.mode, cache.selector, false);
  }

  function clearCache() {
    GM_deleteValue(cacheKey());
    refreshMenu();
    toast('Cleared the cached selector for this site.');
  }

  // ---- picker UI ------------------------------------------------------------------------------

  let picking = false;
  let lastHovered = null;
  // The exact prior value of `lastHovered`'s style attribute (null when it had none at all) — kept
  // off the element itself so the highlight never leaves a trace. Restoring via `el.style.outline =
  // ''` would look right but actually LEAVES a stray `style=""` behind on an element that never had
  // one, which is copied straight into the "clean" HTML we hand back.
  let lastHoveredPrevStyle = null;
  let banner = null;
  let tooltip = null;

  function describe(el) {
    const selector = cssPath(el);
    return `${selector}  (${el.outerHTML.length.toLocaleString()} chars)`;
  }

  function clearHoverHighlight() {
    if (!lastHovered) return;
    if (lastHoveredPrevStyle === null) lastHovered.removeAttribute('style');
    else lastHovered.setAttribute('style', lastHoveredPrevStyle);
    lastHovered = null;
    lastHoveredPrevStyle = null;
  }

  function onHover(e) {
    const el = e.target;
    if (lastHovered === el) return;
    clearHoverHighlight();
    lastHoveredPrevStyle = el.hasAttribute('style') ? el.getAttribute('style') : null;
    lastHovered = el;
    el.style.outline = HIGHLIGHT_OUTLINE;
  }

  function onMove(e) {
    tooltip.style.left = `${e.clientX + 12}px`;
    tooltip.style.top = `${e.clientY + 12}px`;
    tooltip.textContent = describe(e.target);
  }

  function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    const el = e.target;
    const mode = e.shiftKey ? 'inner' : 'outer';
    stopPicking();
    copyAndReport(el, mode, cssPath(el), true);
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); stopPicking(); }
  }

  function startPicking() {
    if (picking) return;
    picking = true;
    document.documentElement.style.cursor = 'crosshair';
    banner = document.createElement('div');
    banner.id = 'cph-banner';
    banner.textContent = 'Click an element to copy its HTML  ·  Shift-click for inner HTML only  ·  Esc to cancel';
    banner.dataset.cphOwn = '1';
    document.documentElement.append(banner);
    tooltip = document.createElement('div');
    tooltip.id = 'cph-tooltip';
    tooltip.dataset.cphOwn = '1';
    document.documentElement.append(tooltip);
    document.addEventListener('mouseover', onHover, true);
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
  }

  function stopPicking() {
    picking = false;
    document.documentElement.style.cursor = '';
    clearHoverHighlight();
    banner?.remove(); banner = null;
    tooltip?.remove(); tooltip = null;
    document.removeEventListener('mouseover', onHover, true);
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('keydown', onKey, true);
  }

  function toast(message, isError) {
    const el = document.createElement('div');
    el.id = 'cph-toast';
    el.textContent = message;
    el.style.background = isError ? '#b00020' : '#1a1a1a';
    document.documentElement.append(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    setTimeout(() => {
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 300);
    }, 2600);
  }

  // banner/tooltip/toast sit above the page (max z-index) and never intercept the mouse — pointer
  // events must reach the real element underneath for hover-highlight and click-to-pick to work.
  const STYLE = `
#cph-banner, #cph-tooltip, #cph-toast {
  position: fixed;
  z-index: 2147483647;
  pointer-events: none;
  font: 13px/1.4 Helvetica, Arial, sans-serif;
}
#cph-banner { top: 0; left: 0; right: 0; padding: 6px 10px; background: #ff5900; color: #fff; text-align: center; }
#cph-tooltip {
  max-width: 60vw; padding: 3px 6px; background: rgba(0, 0, 0, .85); color: #fff; border-radius: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#cph-toast {
  bottom: 16px; right: 16px; padding: 8px 14px; background: #1a1a1a; color: #fff; border-radius: 4px;
  max-width: 40vw; opacity: 0; transition: opacity .3s ease;
}
`;
  document.documentElement.append(Object.assign(document.createElement('style'), { textContent: STYLE }));

  // ---- menu ---------------------------------------------------------------------------------------

  const menuIds = { copy: null, clear: null };

  function refreshMenu() {
    if (menuIds.copy != null) { GM_unregisterMenuCommand(menuIds.copy); menuIds.copy = null; }
    if (menuIds.clear != null) { GM_unregisterMenuCommand(menuIds.clear); menuIds.clear = null; }
    const cache = GM_getValue(cacheKey());
    if (cache) {
      menuIds.copy = GM_registerMenuCommand(`Copy cached HTML (${cache.selector}, ${cache.mode})`, copyFromCache);
      menuIds.clear = GM_registerMenuCommand('Clear cached selector for this site', clearCache);
    }
  }

  GM_registerMenuCommand('Pick element & copy HTML…', startPicking);
  refreshMenu();

  // Expose for manual console use / debugging.
  window.copyPageHtml = { startPicking, copyFromCache, clearCache, cssPath };
})();
