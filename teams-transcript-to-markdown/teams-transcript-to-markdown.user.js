// ==UserScript==
// @name         Teams transcript → Markdown
// @namespace    https://github.com/ingolfbecker/tools
// @version      0.2.0
// @description  On a Stream recording page, scroll the whole (virtualized) transcript, capture every row and download it as clean Markdown, with consecutive speaker turns collapsed and a gap/completeness check.
// @author       Ingolf Becker
// @match        https://*.sharepoint.com/*stream.aspx*
// @grant        GM_registerMenuCommand
// @updateURL    https://raw.githubusercontent.com/watercrossing/tools/main/teams-transcript-to-markdown/teams-transcript-to-markdown.user.js
// @downloadURL  https://raw.githubusercontent.com/watercrossing/tools/main/teams-transcript-to-markdown/teams-transcript-to-markdown.user.js
// @run-at       document-idle
// ==/UserScript==
/*
 * Also works pasted into the DevTools console (e.g. on a page the @match doesn't cover): it then adds the same
 * floating button, and exposes window.TG = { run, status, downloadMarkdown, downloadHTML }.
 */
(function () {
  'use strict';

  // ---- HTML -> Markdown ---------------------------------------------------------------------------------------------

  const GAP_THRESHOLD_SECONDS = 60;
  const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

  const unescape = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) =>
    e[0] !== '#' ? (ENTITIES[e.toLowerCase()] ?? m) : String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : +e.slice(1)));
  const stripTags = (s) => unescape(s.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();

  // '2 hours 57 minutes 5 seconds' / '1 minute 59 seconds' / '3 seconds' -> seconds
  function parseSeconds(t) {
    if (!t) return null;
    const [h, m, s] = ['hour', 'minute', 'second'].map((u) => t.match(new RegExp(`(\\d+)\\s*${u}`)));
    return h || m || s ? (h ? +h[1] : 0) * 3600 + (m ? +m[1] : 0) * 60 + (s ? +s[1] : 0) : null;
  }

  function fmtTs(sec) {
    if (sec == null) return null;
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = String(sec % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  }

  const fmtDur = (sec) => sec >= 60 ? `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s` : `${sec}s`;

  function parse(html) {
    const parts = html.split(/data-list-index="(\d+)"/), entries = [];
    for (let i = 1; i < parts.length; i += 2) {
      const cell = parts[i + 1];
      const name = cell.match(/itemDisplayName-\d+">([^<]*)<\/span>/);
      const ts = cell.match(/baseTimestamp-\d+"><span[^>]*>([^<]*)<\/span>/);
      const txt = cell.match(/(?:sub-entry-\d+)[^>]*class="(?:entryText|eventText)-\d+[^>]*>(.*?)<\/div>/);
      entries.push({ index: +parts[i], name: name ? unescape(name[1]).trim() || null : null, tsSec: ts ? parseSeconds(ts[1]) : null,
                     text: txt ? stripTags(txt[1]) : '', isEvent: !!txt && txt[0].includes('eventText') });
    }
    return entries;
  }

  // Collapse consecutive same-speaker entries, carrying the name forward across header-less continuation rows.
  function buildBlocks(entries) {
    const blocks = [];
    let cur = null;
    for (const e of entries) {
      if (e.isEvent) { blocks.push({ event: e.text, tsSec: e.tsSec }); cur = null; continue; }
      const speaker = cur = e.name || cur || 'Unknown speaker';
      const last = blocks.at(-1);
      if (last && last.speaker === speaker) last.text.push(e.text);
      else blocks.push({ speaker, tsSec: e.tsSec, text: [e.text] });
    }
    return blocks;
  }

  function findGaps(entries, total) {
    const gaps = [], present = entries.map((e) => e.index).sort((a, b) => a - b), presentSet = new Set(present);
    // 1) Missing indices — the definitive completeness check for a virtualized list.
    present.slice(1).forEach((b, i) => {
      const a = present[i];
      if (b - a > 1) gaps.push({ kind: 'missing-entries', detail: `entries ${a + 1}–${b - 1} absent from the DOM (${b - a - 1} entries)` });
    });
    if (present.length && present[0] !== 0) gaps.push({ kind: 'missing-entries', detail: `entries 0–${present[0] - 1} missing before the start` });
    if (present.length && total && present.at(-1) !== total - 1)
      gaps.push({ kind: 'missing-entries', detail: `entries ${present.at(-1) + 1}–${total - 1} missing after the end` });
    // 2) Timestamp jumps. Only header rows carry a timestamp, so a jump with every index present is just a long turn.
    const timed = entries.filter((e) => e.tsSec != null);
    timed.slice(1).forEach((b, i) => {
      const a = timed[i], delta = b.tsSec - a.tsSec;
      if (delta <= GAP_THRESHOLD_SECONDS) return;
      const missingBetween = Array.from({ length: Math.max(0, b.index - a.index - 1) }, (_, k) => a.index + 1 + k).some((x) => !presentSet.has(x));
      const note = missingBetween ? '  <<< spans MISSING entries — likely lost content' : '  (all entries present — probably one long turn)';
      gaps.push({ kind: 'time-jump', detail: `${fmtDur(delta)} between ${fmtTs(a.tsSec)} (entry ${a.index}) and ${fmtTs(b.tsSec)} (entry ${b.index})${note}` });
    });
    return gaps;
  }

  // `meta` is a list of [label, value] pairs shown under the title; empty values are skipped.
  function toMarkdown(html, { title = 'Meeting transcript', meta = [] } = {}) {
    const total = +(html.match(/aria-setsize="(\d+)"/) || [])[1] || null;
    const entries = parse(html), gaps = findGaps(entries, total);
    const speakers = [...new Set(entries.filter((e) => !e.isEvent && e.name).map((e) => e.name))];
    const out = [`# ${title}`, '', ...[...meta, ['Speakers', speakers.join(', ')]].filter(([, v]) => v).map(([k, v]) => `- **${k}:** ${v}`), '',
                 `*${entries.length} of ${total} transcript entries present in the source HTML.*`, ''];
    for (const b of buildBlocks(entries)) {
      const ts = fmtTs(b.tsSec);
      if ('event' in b) out.push(`> _${b.event}_` + (ts ? ` (${ts})` : ''), '');
      else out.push(`**${b.speaker}**` + (ts ? ` (${ts})` : ''), b.text.filter(Boolean).join(' ').trim(), '');
    }
    out.push('---', '', '## Gap / completeness check', '');
    if (!gaps.length) out.push('No gaps >1 min and no missing entries detected. Transcript appears complete.');
    else out.push(`**${gaps.length} issue(s) found** (threshold: ${GAP_THRESHOLD_SECONDS}s):`, '',
                  ...gaps.map((g) => `- **${g.kind === 'missing-entries' ? 'MISSING ENTRIES' : 'TIME GAP'}** — ${g.detail}`));
    out.push('');
    return { markdown: out.join('\n'), entries, total, gaps };
  }

  if (typeof module === 'object' && module.exports) { module.exports = { toMarkdown }; return; } // node, for tests

  // ---- capture the virtualized list ---------------------------------------------------------------------------------

  const LOG = '[TG]';
  const store = new Map();  // data-list-index -> outerHTML (fullest seen)
  let setSize = 0, scroller = null, observer = null, running = false;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function findScroller(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 20) return n;
    }
    return document.scrollingElement || document.documentElement;
  }

  function capture() {
    for (const c of document.querySelectorAll('[data-list-index]')) {
      const idx = +c.getAttribute('data-list-index'), html = c.outerHTML, prev = store.get(idx);
      if (!prev || html.length > prev.length) store.set(idx, html);
    }
    const ss = document.querySelector('[aria-setsize]');
    if (ss) setSize = Math.max(setSize, +ss.getAttribute('aria-setsize'));
  }

  // Keep capturing whenever the list mutates or scrolls (throttled to a frame), so rows Teams destroys are already stored.
  function watch() {
    const anchor = document.querySelector('.ms-List-surface') || document.querySelector('[data-list-index]');
    if (!anchor) return false;
    const s = findScroller(anchor);
    if (s === scroller) return true;
    observer?.disconnect();
    scroller = s;
    let scheduled = false;
    const schedule = () => { if (!scheduled) { scheduled = true; requestAnimationFrame(() => { scheduled = false; capture(); }); } };
    observer = new MutationObserver(schedule);
    observer.observe(scroller, { childList: true, subtree: true });
    scroller.addEventListener('scroll', schedule, { passive: true });
    capture();
    return true;
  }

  const missing = () => Array.from({ length: setSize }, (_, i) => i).filter((i) => !store.has(i));
  const status = () => ({ captured: store.size, setSize, missing: missing() });

  async function autoScroll({ stepFrac = 0.6, delay = 350, maxSteps = 4000, onProgress = () => {} } = {}) {
    scroller.scrollTop = 0;
    await sleep(delay); capture();
    let stall = 0, last = -1;
    for (let i = 0; i < maxSteps; i++) {
      const max = scroller.scrollHeight - scroller.clientHeight;
      scroller.scrollTop = Math.min(scroller.scrollTop + scroller.clientHeight * stepFrac, max);
      await sleep(delay); capture();
      stall = store.size === last ? stall + 1 : 0;
      last = store.size;
      onProgress(store.size, setSize);
      if (setSize && store.size >= setSize) break;
      if (scroller.scrollTop >= max - 2 && stall >= 4) break;  // at the bottom and no new rows
    }
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(delay); capture();
    return status();
  }

  const buildHTML = () => '<div class="ms-List-surface">' + [...store.keys()].sort((a, b) => a - b).map((i) => store.get(i)).join('') + '</div>';

  // The recording's file name, from the ?id= path (".../Recordings/Foo-20240229_142636-Meeting Recording.mp4").
  function recordingName() {
    const id = new URLSearchParams(location.search).get('id');
    const base = id ? id.split('/').pop().replace(/\.[^.]+$/, '') : document.title.replace(/\s*[|–-]\s*Microsoft Stream.*$/i, '');
    return base.trim() || 'transcript';
  }
  const fileSafe = (s) => s.replace(/[\\/:*?"<>|]+/g, '_');

  function save(text, name, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  // Meeting metadata for the header. The file name gives title and start ("<title>-20240229_142636-Meeting Recording");
  // the player gives the duration; SharePoint's REST API, if we may read the file's item, gives who recorded it.
  async function meetingMeta() {
    const name = recordingName(), id = new URLSearchParams(location.search).get('id');
    const m = name.match(/^(.*?)-(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)(?:-Meeting Recording)?$/);
    const video = [...document.querySelectorAll('video')].find((v) => isFinite(v.duration) && v.duration > 0);
    let author = null;
    if (id) {
      try {
        const site = location.pathname.split('/_layouts/')[0];
        const r = await fetch(`${site}/_api/web/GetFileByServerRelativePath(decodedurl='${encodeURIComponent(id.replace(/'/g, "''"))}')?$select=Author/Title&$expand=Author`,
                              { headers: { Accept: 'application/json;odata=nometadata' }, credentials: 'include', signal: AbortSignal.timeout(5000) });
        if (r.ok) author = (await r.json()).Author?.Title || null;
      } catch (e) { console.warn(LOG, 'no file metadata:', e); }
    }
    return {
      title: m ? m[1].trim() : name,
      meta: [
        ['Recorded', m && `${m[2]}-${m[3]}-${m[4]} ${m[5]}:${m[6]}:${m[7]}`],
        ['Duration', video && fmtTs(Math.round(video.duration))],
        ['Recorded by', author],
        ['Recording', id && `${location.origin}${location.pathname}?id=${encodeURIComponent(id)}`],
      ],
    };
  }

  async function downloadMarkdown() {
    const { title, meta } = await meetingMeta(), { markdown, entries, total, gaps } = toMarkdown(buildHTML(), { title, meta });
    save(markdown, fileSafe(recordingName()) + '.md', 'text/markdown');
    console.log(LOG, `downloaded ${entries.length} of ${total} entries; ${gaps.length} gap/completeness issue(s)`, gaps.map((g) => g.detail));
    return gaps;
  }

  const downloadHTML = () => save(buildHTML(), fileSafe(recordingName()) + '.html', 'text/html');

  // ---- UI -----------------------------------------------------------------------------------------------------------

  let button = null;

  async function run() {
    if (running) return;
    if (!watch()) { alert('No transcript found on this page — open the Transcript panel first.'); return; }
    running = true;
    const label = (t) => { if (button) button.textContent = t; };
    try {
      label('Capturing…');
      const st = await autoScroll({ onProgress: (n, t) => label(`Capturing ${n}/${t || '?'}…`) });
      const gaps = await downloadMarkdown();
      const missingCount = st.missing.length;
      label(missingCount ? `⚠ ${missingCount} rows missing — saved anyway` : gaps.length ? `✓ saved (${gaps.length} gap note(s))` : '✓ saved');
    } catch (e) {
      console.error(LOG, e);
      label('✗ failed — see console');
    } finally {
      running = false;
      setTimeout(() => label('Transcript → Markdown'), 6000);
    }
  }

  function addButton() {
    if (button) return;
    button = document.createElement('button');
    button.textContent = 'Transcript → Markdown';
    button.title = 'Scroll the whole transcript, then download it as Markdown';
    Object.assign(button.style, {
      position: 'fixed', right: '16px', bottom: '16px', zIndex: 2147483647, padding: '8px 14px', font: '14px Helvetica, Arial, sans-serif',
      background: '#5b5fc7', color: '#fff', border: 'none', borderRadius: '6px', boxShadow: '0 2px 8px rgba(0,0,0,.3)', cursor: 'pointer',
    });
    button.addEventListener('click', run);
    document.body.append(button);
  }

  // The transcript panel loads (or is opened) well after document-idle, and may live in a frame: show the button only
  // in the document that actually holds transcript rows.
  const poll = setInterval(() => { if (watch()) { addButton(); clearInterval(poll); } }, 1000);
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('Download transcript as Markdown', run);
    GM_registerMenuCommand('Download captured transcript HTML', () => { watch(); capture(); downloadHTML(); });
  }
  window.TG = { run, status, downloadMarkdown, downloadHTML };
})();
