// ==UserScript==
// @name         Overleaf comments export (olc sync)
// @namespace    https://github.com/ingolfbecker/tools
// @version      0.2.0
// @description  Sync Overleaf review comments into the LaTeX source as \olc macros, idempotently, so they land in git.
// @author       Ingolf Becker
// @match        https://www.overleaf.com/project/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';

  // ---- PURE CORE (no DOM, no async, engine-portable) --------------------------------------------

  // Map of the LaTeX special characters to their escaped forms. `\`, `^` and `~` expand to macros
  // that themselves contain `{}`; we therefore escape in a SINGLE pass (see escapeLatex) so those
  // introduced braces are never re-scanned and double-escaped.
  const LATEX_ESCAPES = {
    '\\': '\\textbackslash{}', '{': '\\{', '}': '\\}', '$': '\\$', '&': '\\&',
    '#': '\\#', '^': '\\textasciicircum{}', '_': '\\_', '%': '\\%', '~': '\\textasciitilde{}',
  };

  // Escape arbitrary text for LaTeX. One pass over the characters, looking each up in LATEX_ESCAPES
  // (or keeping it verbatim) — chaining .replace() would re-scan and mangle our own `{}`.
  function escapeLatex(str) {
    return Array.from(str == null ? '' : String(str), ch => LATEX_ESCAPES[ch] || ch).join('');
  }

  // Collapse whitespace. mode "space": every run of newlines/tabs/spaces becomes one space, trimmed
  // (comment text is one line). mode "break": newline runs become a LaTeX line break ` \\ `.
  function collapseWhitespace(str, mode = 'space') {
    const s = str == null ? '' : String(str);
    if (mode === 'break') return s.replace(/[ \t]*(?:\r\n|\r|\n)+[ \t]*/g, ' \\\\ ').replace(/[ \t]+/g, ' ').trim();
    return s.replace(/(?:\r\n|\r|\n|\t| )+/g, ' ').trim();
  }

  // Shorten an over-long highlight for display/storage: with MORE than `max` words, keep the first
  // and last `edge` words joined by an elision marker (`first two [...] last two`); otherwise return
  // it unchanged. Input is assumed whitespace-collapsed (see collapseWhitespace) to single spaces.
  function truncateWords(str, max = 5, edge = 2) {
    const words = (str == null ? '' : String(str)).split(/\s+/).filter(Boolean);
    if (words.length <= max) return words.join(' ');
    return [...words.slice(0, edge), '[...]', ...words.slice(-edge)].join(' ');
  }

  // Format an epoch-ms timestamp as `D MMM, h:mm am/pm` (e.g. `13 May, 6:27 pm`). NEVER pass a
  // `timeZone` option: mini-racer's V8 hangs on it, and honouring the ambient TZ is what we want.
  function formatTimestamp(epochMs) {
    if (typeof epochMs !== 'number' || !isFinite(epochMs)) return '';
    const fmt = new Intl.DateTimeFormat('en-GB',
      { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true });
    const p = {};
    for (const part of fmt.formatToParts(new Date(epochMs))) p[part.type] = part.value;
    return `${p.day} ${p.month}, ${p.hour}:${p.minute} ${(p.dayPeriod || '').toLowerCase()}`;
  }

  // cyrb53 (public-domain non-crypto hash by bryc). Runs two independent 32-bit multiply-xor
  // accumulators over the char codes, then avalanches them into a single 53-bit integer. We render
  // that as base36 (charset [0-9a-z], ~10-11 chars) for a compact, stable content fingerprint.
  function cyrb53(str, seed = 0) {
    let h1 = 0xdeadbeef ^ seed, h2 = 0x41c6ce57 ^ seed;
    for (let i = 0; i < str.length; i++) {
      const ch = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761);
      h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507);
    h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507);
    h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return 4294967296 * (2097151 & h2) + (h1 >>> 0);
  }

  function contentHash(str) {
    return cyrb53(str == null ? '' : String(str)).toString(36);
  }

  // Trailing idempotency marker written after each \olc, on its own line so the `%` only comments
  // out the marker itself. thread/message ids are hex; the hash is base36.
  function buildMarker(threadId, messageId, hash) {
    return `%olcsync:${threadId}:${messageId}:${hash}`;
  }
  const MARKER_RE_SRC = '%olcsync:([0-9a-f]+):([0-9a-f]+):([0-9a-z]+)\\s*$';

  function parseMarker(line) {
    const m = (line == null ? '' : String(line)).match(new RegExp(MARKER_RE_SRC));
    return m ? { threadId: m[1], messageId: m[2], hash: m[3] } : null;
  }

  // Render one \olc macro: \olc{date}{author}{highlight}{comment}. Brace args (not brackets) so it
  // binds to a plain `\providecommand{\olc}[4]{...}` with no packages, and so `]`/`[` in the content
  // (e.g. the `[...]` elision marker, or a `[1]` citation) can't prematurely close an argument.
  // Accepts `comment` or, for annotation objects, falls back to their `text` field; every argument
  // is LaTeX-escaped.
  function renderOlc(ann) {
    const body = ann.comment !== undefined ? ann.comment : (ann.text || '');
    return `\\olc{${escapeLatex(ann.date)}}{${escapeLatex(ann.author)}}` +
           `{${escapeLatex(ann.highlight)}}{${escapeLatex(body)}}`;
  }

  // A full source line: the macro plus its idempotency marker.
  function renderLine(ann) {
    return `${renderOlc(ann)} ${buildMarker(ann.threadId, ann.messageId, ann.hash)}`;
  }

  function authorName(u) {
    if (!u) return 'unknown';
    const name = `${u.first_name || ''} ${u.last_name || ''}`.trim();
    return name || u.email || 'unknown';
  }

  // Join per-file comment positions (op:{c,p,t}) with the project-wide /threads payload into a flat
  // list of annotations, one per message. `threads` is keyed by threadId -> {messages, resolved?};
  // `comments` is the in-memory ranges.comments array. Threads with no position in this file are
  // skipped and counted in `unanchored`. Resolved threads are skipped unless opts.includeResolved.
  function buildAnnotations(threads, comments, opts = {}) {
    const posByThread = {};
    for (const cm of comments || []) {
      if (cm && cm.op && typeof cm.op.t === 'string') posByThread[cm.op.t] = { p: cm.op.p, c: cm.op.c };
    }
    const annotations = [];
    let unanchored = 0;
    for (const [threadId, thread] of Object.entries(threads || {})) {
      // Skip resolved threads (unless opted in) BEFORE the anchor check, so a resolved thread with no
      // position in this file is not also miscounted as `unanchored`.
      if (thread.resolved && !opts.includeResolved) continue;
      const pos = posByThread[threadId];
      if (!pos) { unanchored += 1; continue; }
      const { p } = pos;
      const c = pos.c;
      // Collapse the highlight to a single line, then truncate long spans to `first two [...] last
      // two`, BEFORE hashing/rendering: a span that wraps across two source lines carries a real
      // `\n`, which would otherwise split the \olc line and orphan its marker. offset/anchorOffset
      // keep the ORIGINAL op.p and op.c.length — the anchor is a real position in the untouched
      // document, unaffected by how we shorten the highlight for display.
      const highlight = truncateWords(collapseWhitespace(typeof c === 'string' ? c : '', 'space'));
      for (const msg of thread.messages || []) {
        const date = formatTimestamp(msg.timestamp);
        const author = authorName(msg.user);
        const text = collapseWhitespace(msg.content);
        // Hash the RAW (pre-escape) fields so cosmetic escaping changes never churn the marker.
        const hash = contentHash([date, author, highlight, text].join(' '));
        const ann = {
          threadId, messageId: msg.id, anchorOffset: p,
          offset: p + (typeof c === 'string' ? c.length : 0),
          highlight, date, author, text, hash,
        };
        ann.line = renderLine(ann);
        annotations.push(ann);
      }
    }
    return { annotations, unanchored };
  }

  // Split into lines carrying their [start,end) content offsets (end excludes the newline).
  function scanLines(docText) {
    const lines = [];
    const re = /\r\n|\r|\n/g;
    let start = 0, m;
    while ((m = re.exec(docText)) !== null) {
      lines.push({ text: docText.slice(start, m.index), start, end: m.index });
      start = m.index + m[0].length;
    }
    lines.push({ text: docText.slice(start), start, end: docText.length });
    return lines;
  }

  // Offset of the first character of the line containing `pos` — the position right after the
  // preceding line break (or 0). Inserts are hoisted here so the \olc line lands on its own line(s)
  // ABOVE the annotated line rather than splitting it mid-line. Breaks match scanLines (\r\n|\r|\n).
  function lineStartOffset(docText, pos) {
    const s = docText == null ? '' : String(docText);
    const p = Math.max(0, Math.min(pos | 0, s.length));
    const re = /\r\n|\r|\n/g;
    let start = 0, m;
    while ((m = re.exec(s)) !== null) {
      if (p <= m.index) break;   // pos is on the current line [start, m.index]
      start = m.index + m[0].length;
    }
    return start;
  }

  // Plan idempotent, multi-insert-safe edits. Each annotation lives on its own line, hoisted ABOVE
  // the line it annotates. If a line already carries this thread+message marker: matching hash ->
  // skip, differing hash -> replace that line's content. Otherwise insert at the START of the line
  // holding the highlighted span, grouping same-line inserts into one `\n`-delimited block. Returns
  // edits sorted by `from` DESCENDING so a naive front-to-back apply never invalidates a lower
  // offset. Ranges are non-overlapping.
  function planEdits(docText, annotations, opts = {}) {
    const existing = {};
    for (const ln of scanLines(docText)) {
      const pm = parseMarker(ln.text);
      if (pm) existing[`${pm.threadId}:${pm.messageId}`] = { hash: pm.hash, start: ln.start, end: ln.end };
    }
    const replaces = [];
    const insertsByOffset = new Map();
    for (const ann of annotations) {
      const ex = existing[`${ann.threadId}:${ann.messageId}`];
      if (ex) {
        if (ex.hash === ann.hash) continue;
        replaces.push({ from: ex.start, to: ex.end, insert: ann.line || renderLine(ann) });
      } else {
        // Anchor at the start of the line holding the span (anchorOffset = start of the highlight),
        // so the macro sits on its own line above the text instead of interrupting it.
        const at = lineStartOffset(docText, ann.anchorOffset != null ? ann.anchorOffset : ann.offset);
        if (!insertsByOffset.has(at)) insertsByOffset.set(at, []);
        insertsByOffset.get(at).push(ann.line || renderLine(ann));
      }
    }
    // Build inserts, but drop any whose zero-width offset falls inside a REPLACE range [from,to) —
    // that would emit overlapping edits (pathological: a comment anchored on a previously-injected
    // \olc line that is itself being replaced). Such inserts go into `conflicts` (attached to the
    // returned array) instead, so callers can surface them without corrupting the document.
    const conflicts = [];
    const inserts = [];
    for (const [offset, lns] of insertsByOffset.entries()) {
      const insert = lns.join('\n') + '\n';   // macro line(s) then a newline, pushed in ABOVE the anchor line
      if (replaces.some(r => offset >= r.from && offset < r.to)) conflicts.push({ offset, insert });
      else inserts.push({ from: offset, to: offset, insert });
    }
    const edits = [...replaces, ...inserts].sort((a, b) => b.from - a.from);
    edits.conflicts = conflicts;
    return edits;
  }

  // Apply {from,to,insert} edits to a string. Sorts a copy DESCENDING internally so callers may
  // pass any order; used by tests to simulate the write and prove idempotency.
  function applyEdits(docText, edits) {
    let out = docText;
    for (const e of [...edits].sort((a, b) => b.from - a.from)) out = out.slice(0, e.from) + e.insert + out.slice(e.to);
    return out;
  }

  const CORE = { escapeLatex, collapseWhitespace, truncateWords, formatTimestamp, contentHash,
                 buildMarker, parseMarker, renderOlc, renderLine, lineStartOffset,
                 buildAnnotations, planEdits, applyEdits, MARKER_RE_SRC };

  // ---- test export hatch: MUST be before any DOM/browser code -----------------------------------
  if (typeof module !== 'undefined' && module.exports && typeof window === 'undefined') {
    module.exports = CORE; return;
  }

  // ---- BROWSER INTEGRATION (DOM / CM6 / menu / fetch) -------------------------------------------

  const LOG = '[olc]';

  // The live CM6 EditorView, reached through the .cm-content DOM node's ContentView back-pointer.
  function getEditorView() {
    const view = document.querySelector('.cm-content')?.cmView?.view;
    if (!view) {
      console.error(`${LOG} could not reach the CodeMirror EditorView (.cm-content?.cmView?.view). ` +
                    `Is a .tex file open, and have Overleaf's CM6 internals shifted?`);
      return null;
    }
    return view;
  }

  // The per-file comment ranges live in one of the CM6 state fields. Find the first value carrying
  // both a comments array and a docId — do NOT hardcode the field index, it is not stable.
  function getRangesState(view) {
    const values = view?.state?.values;
    if (!Array.isArray(values)) { console.error(`${LOG} EditorView has no state.values array`); return null; }
    for (const v of values) {
      if (v && v.ranges && Array.isArray(v.ranges.comments) && v.ranges.docId) {
        return { comments: v.ranges.comments, docId: v.ranges.docId };
      }
    }
    console.error(`${LOG} no ranges state field (needs .ranges.comments + .ranges.docId) in view.state.values`);
    return null;
  }

  function projectId() {
    return location.pathname.split('/')[2];
  }

  function fetchThreads(pid) {
    return fetch(`/project/${pid}/threads`, { credentials: 'include' }).then(r => r.json());
  }

  // Best-effort track-changes sniff. We cannot read Overleaf's React state, so we look for tracked
  // change decorations / an engaged review toggle in the DOM. Treat a positive as advisory only.
  function trackChangesLikelyOn() {
    const toggle = document.querySelector('[aria-label*="track change" i]');
    if (toggle && (toggle.getAttribute('aria-pressed') === 'true' || toggle.getAttribute('aria-checked') === 'true')) {
      return true;
    }
    return !!document.querySelector('.ol-cm-change, ins.ol-cm-change, .track-changes-marker');
  }

  // Count how each annotation would land, for the console summary and confirm dialog.
  function classify(docText, annotations) {
    const byKey = {};
    for (const ln of docText.split(/\r\n|\r|\n/)) {
      const pm = parseMarker(ln);
      if (pm) byKey[`${pm.threadId}:${pm.messageId}`] = pm.hash;
    }
    let inserts = 0, replaces = 0, skips = 0;
    for (const a of annotations) {
      const h = byKey[`${a.threadId}:${a.messageId}`];
      if (h === undefined) inserts += 1;
      else if (h === a.hash) skips += 1;
      else replaces += 1;
    }
    return { inserts, replaces, skips };
  }

  // Plan + (after confirmation) apply the sync for a single already-open document/view.
  function syncViewWithThreads(view, threads, opts) {
    const rs = getRangesState(view);
    if (!rs) return { applied: false, reason: 'no-ranges' };
    const { annotations, unanchored } = buildAnnotations(threads, rs.comments, opts);
    const docText = view.state.doc.toString();
    const edits = planEdits(docText, annotations, opts);
    const { inserts, replaces, skips } = classify(docText, annotations);

    annotations.forEach(a => console.log(`${LOG} ${a.line}`));
    console.log(`${LOG} summary: inserts=${inserts} replaces=${replaces} skips=${skips} ` +
                `unanchored=${unanchored} (docId=${rs.docId})`);

    if (!edits.length) { console.log(`${LOG} nothing to sync for this file`); return { applied: false, reason: 'noop' }; }
    if (trackChangesLikelyOn()) {
      console.warn(`${LOG} track-changes appears to be ON — injected lines may be recorded as tracked changes.`);
    }
    const ok = confirm(
      `Overleaf comment sync\n\n` +
      `About to write ${inserts} new and ${replaces} updated \\olc line(s) into the OPEN file.\n\n` +
      `This is a REAL edit to the live CodeMirror document: it propagates to every collaborator on ` +
      `this project in real time, and will be captured by track-changes if that is enabled.\n\n` +
      `${unanchored} thread(s) have no anchor in this file and are skipped.\n\nProceed?`);
    if (!ok) { console.log(`${LOG} cancelled by user`); return { applied: false, reason: 'cancelled' }; }

    // CM6 wants edits in document order and remaps offsets internally.
    const changes = [...edits].sort((a, b) => a.from - b.from).map(e => ({ from: e.from, to: e.to, insert: e.insert }));
    view.dispatch({ changes });
    console.log(`${LOG} applied ${changes.length} change(s) to docId=${rs.docId}`);
    return { applied: true, inserts, replaces, skips, unanchored };
  }

  async function syncThisFile(opts = {}) {
    const view = getEditorView();
    if (!view) return;
    let threads;
    try {
      threads = await fetchThreads(projectId());
    } catch (e) { console.error(`${LOG} failed to fetch /threads`, e); return; }
    syncViewWithThreads(view, threads, opts);
  }

  // ---- "all files" (EXPERIMENTAL) ---------------------------------------------------------------
  // There is no project-level ranges/positions endpoint: per-file positions load only when a doc is
  // open. So we walk the .tex entries in the file tree, click each open, wait for its ranges to load,
  // sync it, then restore the originally-open document. Fragile and DOM-driven; fails loudly.

  function waitFor(pred, { timeout = 8000, interval = 100 } = {}) {
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      const tick = () => {
        let v; try { v = pred(); } catch (e) { v = null; }
        if (v) return resolve(v);
        if (Date.now() - t0 > timeout) return reject(new Error('timeout'));
        setTimeout(tick, interval);
      };
      tick();
    });
  }

  function texFileTreeItems() {
    const items = [...document.querySelectorAll('.file-tree [role="treeitem"], .file-tree li.entity')];
    return items.filter(el => /\.tex\b/i.test(el.textContent || ''));
  }

  async function syncAllFiles(opts = {}) {
    if (!confirm('EXPERIMENTAL: sync comments across ALL .tex files.\n\n' +
                 'This opens every .tex file in turn and writes into each live document (propagating ' +
                 'to collaborators). You will still confirm each file. Continue?')) return;
    const view = getEditorView();
    if (!view) return;
    const startState = getRangesState(view);
    const items = texFileTreeItems();
    if (!items.length) { console.error(`${LOG} could not find .tex entries in the file tree — Overleaf DOM may have changed.`); return; }

    let threads;
    try { threads = await fetchThreads(projectId()); }
    catch (e) { console.error(`${LOG} failed to fetch /threads`, e); return; }

    const startLabel = items.find(el => (el.textContent || '').trim() &&
      startState && el.getAttribute('aria-selected') === 'true');
    for (const item of items) {
      const clickable = item.querySelector('[role="button"], button, a, .entity-name') || item;
      clickable.click();
      let rs;
      try {
        rs = await waitFor(() => {
          const v = getEditorView(); const s = v && getRangesState(v);
          return s && startState && s.docId !== undefined ? s : null;
        });
      } catch (e) { console.warn(`${LOG} timed out opening`, item.textContent, '- skipping'); continue; }
      const v = getEditorView();
      if (v) syncViewWithThreads(v, threads, opts);
    }
    if (startLabel) (startLabel.querySelector('[role="button"], button, a, .entity-name') || startLabel).click();
    console.log(`${LOG} all-files sync finished (experimental)`);
  }

  // ---- Menu injection ---------------------------------------------------------------------------

  const MENU_ID = 'olc-comment-sync-menu';
  let warnedNoAnchor = false;

  // Find Overleaf's top-nav "Help" control to anchor our dropdown beside it.
  function findHelpAnchor() {
    const nav = document.querySelector('nav, header, .toolbar-header, .ol-cm-toolbar') || document;
    const els = [...nav.querySelectorAll('a, button, [role="menuitem"], [role="button"]')];
    return els.find(el => (el.textContent || '').trim().toLowerCase() === 'help') || null;
  }

  // Overleaf's native menu-bar toggles (File/Edit/…) carry these classes; matching them means our
  // button is styled entirely by THEIR CSS variables (colour, hover, active, padding, caret) with no
  // hardcoded values. Used only as a fallback — we normally clone the class list off a live sibling.
  const NATIVE_TOGGLE_CLASS =
    'ide-redesign-toolbar-dropdown-toggle-subdued ide-redesign-toolbar-button-subdued ' +
    'menu-bar-toggle dropdown-toggle btn btn-secondary';

  function buildMenu(anchor) {
    const wrap = document.createElement('div');
    wrap.id = MENU_ID;
    wrap.className = 'dropdown';   // Bootstrap: position:relative, the anchor for the absolute menu

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Comment sync';
    // Clone the class list from a real sibling toggle (the located "Help" button) so we track their
    // styling exactly and self-heal if they rename classes; strip transient state, fall back if absent.
    const nativeBtn = anchor ? (anchor.closest('button') || anchor) : null;
    const rawCls = nativeBtn && typeof nativeBtn.className === 'string' ? nativeBtn.className : '';
    const cls = rawCls.split(/\s+/).filter(c => c && c !== 'show' && c !== 'active').join(' ');
    btn.className = cls || NATIVE_TOGGLE_CLASS;
    btn.setAttribute('aria-haspopup', 'true');
    btn.setAttribute('aria-expanded', 'false');

    // The dropdown itself: Overleaf's own dropdown-menu / dropdown-item classes, so the panel, rows,
    // hover and dividers are themed by their variables. We don't run Bootstrap's Popper, so we pin the
    // menu directly under the toggle ourselves (and cancel any popper transform the class carries).
    const menu = document.createElement('ul');
    menu.className = 'dropdown-menu-popper dropdown-menu';
    menu.setAttribute('role', 'menu');
    Object.assign(menu.style, { position: 'absolute', top: '100%', right: 'auto', bottom: 'auto',
                                left: '0', margin: '0', transform: 'none', display: 'none' });

    const setOpen = open => {
      for (const el of [wrap, btn, menu]) el.classList.toggle('show', open);
      btn.setAttribute('aria-expanded', String(open));
      // The `dropdown-menu-popper` class ships `visibility:hidden` — it assumes Popper reveals the menu
      // once positioned. We pin the menu ourselves (no Popper), so we must clear that visibility too,
      // otherwise the panel is laid out (display:block, correct size) but invisible.
      menu.style.display = open ? 'block' : 'none';
      menu.style.visibility = open ? 'visible' : '';
      if (!open) return;
      // No Popper to flip us: left-align by default, but if that spills past the viewport, right-align.
      Object.assign(menu.style, { left: '0', right: 'auto' });
      if (menu.getBoundingClientRect().right > window.innerWidth - 8) {
        Object.assign(menu.style, { left: 'auto', right: '0' });
      }
    };

    const mkItem = (label, icon, fn) => {
      const a = document.createElement('a');
      a.className = 'dropdown-item';
      a.setAttribute('role', 'menuitem');
      a.setAttribute('tabindex', '0');
      a.href = '#';
      const ic = document.createElement('span');
      ic.className = 'material-symbols dropdown-item-leading-icon';
      ic.setAttribute('aria-hidden', 'true');
      ic.setAttribute('translate', 'no');
      ic.textContent = icon;
      a.append(ic, document.createTextNode(label));
      a.addEventListener('click', e => { e.preventDefault(); setOpen(false); fn(); });
      return a;
    };

    menu.append(
      mkItem('Sync comments to tex on this file', 'sync', () => syncThisFile()),
      mkItem('Send comments to tex on all files (experimental)', 'sync_alt', () => syncAllFiles()));

    btn.addEventListener('click', e => { e.stopPropagation(); setOpen(!wrap.classList.contains('show')); });
    document.addEventListener('click', () => setOpen(false));
    wrap.append(btn, menu);
    return wrap;
  }

  function injectMenu() {
    if (document.getElementById(MENU_ID)) return;
    const help = findHelpAnchor();
    if (!help) {
      if (!warnedNoAnchor) {
        console.error(`${LOG} could not find the "Help" nav item to anchor the Comment sync menu — Overleaf UI may have changed.`);
        warnedNoAnchor = true;
      }
      return;
    }
    warnedNoAnchor = false;
    const host = help.closest('li') || help.parentElement || help;
    host.parentElement.insertBefore(buildMenu(help), host);
  }

  // Re-inject across SPA route changes / re-renders; the id guard prevents duplicates.
  function startMenu() {
    injectMenu();
    const obs = new MutationObserver(() => injectMenu());
    obs.observe(document.body, { childList: true, subtree: true });
    setInterval(injectMenu, 3000);
  }

  // Expose for manual console use / debugging.
  window.olcSync = { syncThisFile, syncAllFiles, CORE };
  startMenu();
})();
