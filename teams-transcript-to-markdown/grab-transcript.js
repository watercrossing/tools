/*
 * Teams transcript grabber — paste into the browser DevTools console while the
 * transcript panel is open. It watches the virtualized list and accumulates
 * every .ms-List-cell (keyed by data-list-index, so duplicates are de-duped)
 * even as Teams destroys off-screen rows.
 *
 * Usage:
 *   1. Paste this whole file into the console and press Enter.
 *   2. Run:  await TG.auto()      // scrolls top->bottom, capturing everything
 *   3. Run:  TG.status()          // shows captured / total and any missing indices
 *   4. Run:  TG.download()        // saves transcript.html   (or TG.copy() to clipboard)
 *
 * Then convert the saved file to Markdown:
 *   uv run teams-transcript-to-markdown.py transcript.html transcript.md
 *
 * If step 1 logs "no transcript cells found", the panel is in an iframe: pick the
 * correct frame in the console's context dropdown (top-left) and paste again.
 */
(() => {
  const store = new Map();          // data-list-index -> outerHTML (longest seen)
  let setSize = 0;                  // total entries, from aria-setsize
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function findScroller(el) {
    let node = el;
    while (node && node !== document.body) {
      const s = getComputedStyle(node);
      if (/(auto|scroll)/.test(s.overflowY) &&
          node.scrollHeight > node.clientHeight + 20) return node;
      node = node.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function capture() {
    for (const c of document.querySelectorAll('[data-list-index]')) {
      const idx = +c.getAttribute('data-list-index');
      const html = c.outerHTML;
      const prev = store.get(idx);
      if (!prev || html.length > prev.length) store.set(idx, html); // keep fullest
    }
    const ss = document.querySelector('[aria-setsize]');
    if (ss) setSize = Math.max(setSize, +ss.getAttribute('aria-setsize'));
  }

  // Keep capturing whenever the list mutates or scrolls (throttled to a frame).
  const anchor = document.querySelector('.ms-List-surface') ||
                 document.querySelector('[data-list-index]');
  if (!anchor) { console.warn('TG: no transcript cells found — wrong frame?'); return; }
  const scroller = findScroller(anchor);
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; capture(); });
  };
  const obs = new MutationObserver(schedule);
  obs.observe(scroller, { childList: true, subtree: true });
  scroller.addEventListener('scroll', schedule, { passive: true });
  capture();

  function missing() {
    const m = [];
    for (let i = 0; i < setSize; i++) if (!store.has(i)) m.push(i);
    return m;
  }

  function status() {
    const m = missing();
    console.log(`TG: captured ${store.size} of ${setSize || '?'} — ` +
                (m.length ? `${m.length} missing: ${summariseRanges(m)}`
                          : 'complete ✓'));
    return { captured: store.size, setSize, missing: m };
  }

  function summariseRanges(nums) {
    const parts = [];
    let start = nums[0], prev = nums[0];
    for (const n of nums.slice(1)) {
      if (n === prev + 1) { prev = n; continue; }
      parts.push(start === prev ? `${start}` : `${start}-${prev}`);
      start = prev = n;
    }
    parts.push(start === prev ? `${start}` : `${start}-${prev}`);
    return parts.join(', ');
  }

  async function auto({ stepFrac = 0.6, delay = 350, maxSteps = 4000 } = {}) {
    console.log('TG: auto-scroll starting…');
    scroller.scrollTop = 0;
    await sleep(delay); capture();
    let stall = 0, last = -1;
    for (let i = 0; i < maxSteps; i++) {
      const max = scroller.scrollHeight - scroller.clientHeight;
      scroller.scrollTop = Math.min(scroller.scrollTop + scroller.clientHeight * stepFrac, max);
      await sleep(delay); capture();
      stall = store.size === last ? stall + 1 : 0;
      last = store.size;
      if (i % 5 === 0) console.log(`TG: …${store.size}/${setSize || '?'}`);
      if (setSize && store.size >= setSize) break;
      if (scroller.scrollTop >= max - 2 && stall >= 4) break; // at bottom & no new rows
    }
    // settle at the very bottom, then confirm the top rows are still stored
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(delay); capture();
    return status();
  }

  function buildHTML() {
    const idxs = [...store.keys()].sort((a, b) => a - b);
    return '<div class="ms-List-surface">' +
           idxs.map((i) => store.get(i)).join('') + '</div>';
  }

  function download(name = 'transcript.html') {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([buildHTML()], { type: 'text/html' }));
    a.download = name; a.click(); URL.revokeObjectURL(a.href);
    console.log(`TG: downloaded ${name} (${store.size} entries)`);
  }

  async function copy() {
    await navigator.clipboard.writeText(buildHTML());
    console.log(`TG: copied ${store.size} entries to clipboard`);
  }

  function stop() { obs.disconnect(); console.log('TG: stopped watching.'); }

  window.TG = { auto, status, download, copy, stop, capture, store, buildHTML };
  console.log('TG ready. Run:  await TG.auto()   then  TG.download()');
})();
