// ==UserScript==
// @name         Teams chat → Markdown
// @namespace    https://github.com/ingolfbecker/tools
// @version      0.1.0
// @description  In a Teams chat or meeting chat, scroll back through the whole (virtualized, lazily loaded) conversation, capture every message and download it as clean Markdown, with authors, times, reply-quotes, reactions, emoji and links.
// @author       Ingolf Becker
// @match        https://teams.microsoft.com/*
// @match        https://teams.cloud.microsoft/*
// @match        https://teams.live.com/*
// @grant        GM_registerMenuCommand
// @updateURL    https://raw.githubusercontent.com/watercrossing/tools/main/teams-chat-to-markdown/teams-chat-to-markdown.user.js
// @downloadURL  https://raw.githubusercontent.com/watercrossing/tools/main/teams-chat-to-markdown/teams-chat-to-markdown.user.js
// @run-at       document-idle
// ==/UserScript==
/*
 * Also works pasted into the DevTools console: it then adds the same floating button, and exposes
 * window.TC = { run, status, downloadMarkdown, downloadHTML }.
 */
(function () {
  'use strict';

  // ---- HTML -> Markdown ---------------------------------------------------------------------------------------------
  // Anchors on stable data-tid / id / data-mid attributes, never on Teams' generated CSS class names.

  const Q = '\u0000Q\u0000';  // marks lines we emit as Markdown structure, so prose-escaping leaves them alone
  const RUNWAY = '[data-tid="message-pane-list-runway"]';
  const TIME_PREFIX = /^[^:]{0,24}?\b\d{1,2}:\d{2}\s+/;  // "Yesterday 14:59 Meeting started": the time is ours to add

  const pad = (n) => String(n).padStart(2, '0');
  const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const longDate = (d, weekday) =>
    d.toLocaleDateString('en-GB', { weekday: weekday ? 'long' : undefined, day: 'numeric', month: 'long', year: 'numeric' });
  const squash = (s) => s.replace(/\s+/g, ' ').trim();
  const byTid = (el, tid) => el.querySelector(`[data-tid="${tid}"]`);

  // Wrap inline text in a marker, keeping surrounding whitespace outside it ("**bold** text", not "**bold **text").
  function wrap(text, mark) {
    const [, pre, body, post] = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
    return body ? pre + mark + body + mark + post : text;
  }

  function link(a, ctx) {
    const text = squash(serialize(a, ctx)), href = a.getAttribute('href') || '';
    if (!href || href === text || href === `mailto:${text}`) return text || href;
    return `[${text || href}](${href})`;
  }

  // A reply-quote card. Teams only keeps a truncated preview of the quoted message (ending in "…").
  function quote(card, ctx) {
    const ts = byTid(card, 'quoted-reply-timestamp'), pv = byTid(card, 'quoted-reply-preview-content');
    const author = [...card.querySelectorAll('span')]
      .find((s) => s !== ts && !s.contains(ts) && !(pv && (s.contains(pv) || pv.contains(s))) && s.textContent.trim());
    // "09/11/2026 16:10" -> "16:10" when the quoted message is from the same day as the reply (D/M or M/D, whichever matches).
    let time = ts ? squash(ts.textContent) : '';
    const m = time.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}:\d{2})$/);
    if (m && [[m[2], m[1]], [m[1], m[2]]].some(([mo, d]) => `${m[3]}-${pad(mo)}-${pad(d)}` === ctx.day)) time = m[4];
    const head = `**${author ? squash(author.textContent) : 'Unknown'}**` + (time ? ` · ${time}` : '');
    return `\n\n${Q}> ${head}: ${pv ? squash(serialize(pv, ctx)) : ''}\n\n`;
  }

  function element(el, ctx) {
    if (el.getAttribute('data-tid') === 'quoted-reply-card') return quote(el, ctx);
    const tag = el.tagName.toLowerCase(), inner = () => serialize(el, ctx);
    switch (tag) {
      case 'img': { const alt = el.getAttribute('alt'); return alt && alt !== 'image' ? alt : '*(image)*'; }  // emoji live in the alt text
      case 'a': return link(el, ctx);
      case 'br': return '\n';
      case 'p': return `\n\n${inner().trim()}\n\n`;
      case 'div': return el.closest('p') || el.hasAttribute('data-mention-type') ? inner() : `\n${inner()}\n`;  // @mentions are divs
      case 'b': case 'strong': return wrap(inner(), '**');
      case 'i': case 'em': return wrap(inner(), '_');
      case 's': case 'strike': case 'del': return wrap(inner(), '~~');
      case 'code': return el.closest('pre') ? inner() : wrap(inner(), '`');
      case 'pre': return `\n\n${['```', ...el.textContent.replace(/\n$/, '').split('\n'), '```'].map((l) => Q + l).join('\n')}\n\n`;
      case 'blockquote': return `\n\n${finish(inner()).split('\n').map((l) => `${Q}> ${l}`).join('\n')}\n\n`;
      case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return `\n\n${wrap(inner().trim(), '**')}\n\n`;
      case 'ul': case 'ol': return `\n\n${[...el.children].filter((li) => li.tagName.toLowerCase() === 'li').map((li, i) =>
        `${Q}${tag === 'ol' ? `${i + 1}.` : '-'} ${finish(serialize(li, ctx)).replace(/\n+/g, '\n   ')}`).join('\n')}\n\n`;
      case 'script': case 'style': case 'svg': case 'button': return '';
      default: return inner();
    }
  }

  function serialize(node, ctx) {
    return [...node.childNodes].map((c) => c.nodeType === 3 ? c.nodeValue : c.nodeType === 1 ? element(c, ctx) : '').join('');
  }

  // Tidy whitespace, and escape a leading '#' or '>' in prose so it isn't read as a heading or quote.
  function finish(text) {
    text = text.split('\n').map((l) => l.replace(/[ \t ]+$/, '')).join('\n').replace(/\n{3,}/g, '\n\n').trim();
    return text.split('\n').map((l) => l.startsWith(Q) ? l.slice(Q.length) : /^\s*[#>]/.test(l) ? '\\' + l.trimStart() : l).join('\n');
  }

  function reactions(row) {
    return [...row.querySelectorAll('[data-tid="diverse-reaction-pill-button"]')].map((pill) => {
      const txt = squash(pill.textContent), count = (txt.match(/^\d+/) || [''])[0];
      const emoji = pill.querySelector('img')?.getAttribute('alt') || txt.replace(/^\d+\s*/, '').replace(/\s*reactions?\.?$/i, '') || 'reaction';
      return emoji + (count ? ` ${count}` : '');
    });
  }

  const rowOf = (el, list) => { while (el.parentElement && el.parentElement !== list) el = el.parentElement; return el; };

  // Every message and control notice ("Meeting started", "X named the meeting Y"), keyed and ordered by its epoch-ms id.
  function parse(root) {
    const list = root.querySelector(RUNWAY) || root, items = new Map();
    for (const body of list.querySelectorAll('[data-mid]')) {
      const ts = +body.getAttribute('data-mid'), row = rowOf(body, list), d = new Date(ts);
      if (!ts || items.has(ts)) continue;
      const ctrl = row.querySelector(`[id="content-control-message-${ts}"]`), content = row.querySelector(`[id="content-${ts}"]`);
      if (ctrl) { items.set(ts, { ts, d, ctrl: squash(ctrl.textContent).replace(TIME_PREFIX, '') }); continue; }
      if (!content) continue;
      let text = finish(serialize(content, { day: dayKey(d) }));
      // File attachments normally also appear as a link in the text; list any that don't.
      const files = [...row.querySelectorAll(`[id="attachments-${ts}"] [aria-label][numberoffiles]`)].map((f) => f.getAttribute('aria-label'));
      text = [text, ...files.filter((f) => !text.includes(f)).map((f) => `📎 ${f}`)].filter(Boolean).join('\n\n');
      items.set(ts, { ts, d, text, author: squash(row.querySelector(`[id="author-${ts}"]`)?.textContent || ''),
                      edited: !!row.querySelector(`[id="edited-${ts}"]`), reactions: reactions(row) });
    }
    const out = [...items.values()].sort((a, b) => a.ts - b.ts);
    // A follow-on message from the same person may come without its own author line.
    let last = 'Unknown';
    for (const m of out) if (!m.ctrl) last = m.author ||= last;
    return out;
  }

  // `root` is a Document or Element holding the message list; `notes` are lines shown in italics under the header.
  function toMarkdown(root, { title = '', notes = [] } = {}) {
    const items = parse(root), msgs = items.filter((i) => !i.ctrl);
    const named = items.map((i) => i.ctrl?.match(/named the meeting\s+(.+?)\s*\.?$/)?.[1]).filter(Boolean).at(-1);
    const [first, last] = [msgs[0], msgs.at(-1)].map((m) => m && longDate(m.d));
    const participants = [...new Set(msgs.map((m) => m.author))];
    const out = [`# ${named || title || 'Teams chat'}`, '',
                 ...[['Date', first === last ? first : `${first} – ${last}`], ['Messages', msgs.length], ['Participants', participants.join(', ')]]
                   .filter(([, v]) => v).map(([k, v]) => `- **${k}:** ${v}`), '', ...notes.flatMap((n) => [`*${n}*`, ''])];
    let day = null;
    for (const i of items) {
      if (dayKey(i.d) !== day) out.push(`## ${longDate(i.d, true)}`, ''), day = dayKey(i.d);
      if (i.ctrl) { out.push(`*${hhmm(i.d)} — ${i.ctrl}*`, ''); continue; }
      out.push(`**${i.author}** · ${hhmm(i.d)}${i.edited ? ' · edited' : ''}`, '', i.text || '*(no text)*', '');
      if (i.reactions.length) out.push(`_Reactions: ${i.reactions.join(' · ')}_`, '');
    }
    return { markdown: out.join('\n').trimEnd() + '\n', title: named || title, messages: msgs.length, participants,
             firstDay: msgs[0] && dayKey(msgs[0].d) };
  }

  if (typeof module === 'object' && module.exports) { module.exports = { toMarkdown }; return; } // node, for tests

  // ---- capture the virtualized, lazily loaded list ------------------------------------------------------------------

  const LOG = '[TC]';
  const store = new Map();  // data-mid -> outerHTML of its row (fullest seen)
  let runway = null, scroller = null, observer = null, running = false;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function findScroller(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 20) return n;
    }
    return document.scrollingElement || document.documentElement;
  }

  function capture() {
    if (!runway?.isConnected) return;
    for (const body of runway.querySelectorAll('[data-mid]')) {
      const mid = body.getAttribute('data-mid'), html = rowOf(body, runway).outerHTML, prev = store.get(mid);
      if (!prev || html.length > prev.length) store.set(mid, html);
    }
  }

  // Capture whenever the list mutates (throttled to a frame), so rows Teams destroys while scrolling are already stored.
  function attach() {
    const r = document.querySelector(RUNWAY);
    if (!r) return false;
    if (r === runway && scroller?.isConnected) return true;
    observer?.disconnect();
    runway = r;
    scroller = findScroller(r);
    let scheduled = false;
    observer = new MutationObserver(() => { if (!scheduled) { scheduled = true; requestAnimationFrame(() => { scheduled = false; capture(); }); } });
    observer.observe(runway, { childList: true, subtree: true });
    return true;
  }

  const status = () => ({ captured: store.size });
  const loading = () => [...document.querySelectorAll('[data-testid="vl-placeholders"]')].some((e) => e.offsetHeight > 0);

  // Scroll until the list stops moving and growing. Upwards, Teams fetches older messages from the server as you near the
  // top; whether the list scrolls normally or is flex-reversed (negative scrollTop), "didn't move" is how we know we're there.
  async function scrollTo(up, { delay = 400, settle = 6, maxSteps = 5000, onProgress = () => {} } = {}) {
    let stall = 0;
    for (let i = 0; i < maxSteps; i++) {
      const top = scroller.scrollTop, height = scroller.scrollHeight, size = store.size;
      scroller.scrollTop = up ? top - scroller.clientHeight * 0.6 : scroller.scrollHeight;
      await sleep(delay); capture();
      onProgress(store.size);
      const moved = Math.abs(scroller.scrollTop - top) > 1 || scroller.scrollHeight !== height || store.size !== size;
      stall = moved ? 0 : stall + 1;
      if (stall >= (loading() ? settle * 5 : settle)) return true;
    }
    return false;
  }

  const buildHTML = () => `<div data-tid="message-pane-list-runway">${[...store.keys()].sort((a, b) => a - b).map((k) => store.get(k)).join('')}</div>`;

  const chatTitle = () => document.title.split(' | ').map((s) => s.trim()).filter((s) => s && !/^(chat|microsoft teams)$/i.test(s))[0] || '';
  const fileSafe = (s) => s.replace(/[\\/:*?"<>|]+/g, '_').trim();

  function save(text, name, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  async function downloadMarkdown({ reachedStart = true } = {}) {
    const doc = new DOMParser().parseFromString(buildHTML(), 'text/html');
    const notes = reachedStart ? [] : ['⚠ The capture did not reach the start of the chat; earlier messages may be missing.'];
    const r = toMarkdown(doc, { title: chatTitle(), notes });
    save(r.markdown, fileSafe(`${r.title || 'Teams chat'} ${r.firstDay || ''}`) + '.md', 'text/markdown');
    console.log(LOG, `downloaded ${r.messages} messages from ${r.participants.length} participants`);
    return r;
  }

  const downloadHTML = () => save(buildHTML(), fileSafe(chatTitle() || 'Teams chat') + '.html', 'text/html');

  // ---- UI -----------------------------------------------------------------------------------------------------------

  const LABEL = 'Chat → Markdown';
  let button = null;
  const label = (t) => { if (button) button.textContent = t; };

  async function run() {
    if (running) return;
    if (!attach()) { alert('No chat found on this page — open a chat first.'); return; }
    running = true;
    store.clear();
    try {
      label('Capturing…');
      await scrollTo(false, { settle: 3 });  // to the newest message first, in case the chat was opened partway up
      const reachedStart = await scrollTo(true, { onProgress: (n) => label(`Capturing ${n}…`) });
      const r = await downloadMarkdown({ reachedStart });
      label(reachedStart ? `✓ saved ${r.messages} messages` : `⚠ start not reached — saved ${r.messages}`);
      scrollTo(false, { settle: 3 });  // leave the chat at the newest message again
    } catch (e) {
      console.error(LOG, e);
      label('✗ failed — see console');
    } finally {
      running = false;
      setTimeout(() => label(LABEL), 6000);
    }
  }

  // Teams' compose box and header buttons fill the page edges, so sit the button in the top-right corner of the message list
  // itself, and hide it while no chat is open.
  function place() {
    const r = document.querySelector(RUNWAY), box = r && findScroller(r).getBoundingClientRect();
    if (!button) {
      button = document.createElement('button');
      button.textContent = LABEL;
      button.title = 'Scroll back through the whole chat, then download it as Markdown';
      Object.assign(button.style, {
        position: 'fixed', zIndex: 2147483647, padding: '6px 12px', font: '13px Helvetica, Arial, sans-serif', background: '#5b5fc7',
        color: '#fff', border: 'none', borderRadius: '6px', boxShadow: '0 2px 8px rgba(0,0,0,.3)', cursor: 'pointer', opacity: '.75',
      });
      button.addEventListener('mouseenter', () => { button.style.opacity = '1'; });
      button.addEventListener('mouseleave', () => { button.style.opacity = '.75'; });
      button.addEventListener('click', run);
      document.body.append(button);
    }
    button.style.display = box?.width || running ? '' : 'none';
    if (box?.width) Object.assign(button.style, { top: `${Math.max(box.top, 0) + 8}px`, right: `${Math.max(innerWidth - box.right, 0) + 24}px` });
  }

  // Teams is a single-page app: chats come and go without a page load, so keep checking.
  setInterval(place, 1000);
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('Download chat as Markdown', run);
    GM_registerMenuCommand('Download captured chat HTML', () => { if (attach()) { capture(); downloadHTML(); } });
  }
  window.TC = { run, status, downloadMarkdown, downloadHTML };
})();
