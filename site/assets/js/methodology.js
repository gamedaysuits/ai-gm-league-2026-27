// Methodology: table of contents, copy buttons, drawn draft order, ledger head + in-browser verification.
import './chrome.js';
import { $, $$, html, mount, loadManifest, loadData, buildLeague, crest, fetchText, fmtWhen, ICONS } from './lib.js';

const manifest = await loadManifest();

/* ------------------------------------------------------------------ table of contents */
function buildToc() {
  const main = $('#method-main');
  const toc = $('#toc');
  const heads = $$('h2, h3', main).filter((h) => h.id && !h.closest('.chain'));
  if (!heads.length) return;
  const label = (h) => {
    if (h.tagName === 'H2' && h.closest('#rules')) return 'The rules';
    if (h.tagName === 'H2' && h.closest('#draft-order')) return 'Draft order';
    return h.textContent.replace(/\s*\(.*\)\s*$/, '');
  };
  mount(toc, html`<h2>On this page</h2><ol>${heads.map((h) => html`<li class="${h.tagName === 'H3' ? 'toc-sub' : ''}"><a href="#${h.id}">${label(h)}</a></li>`)}</ol>`);
  const links = new Map($$('a', toc).map((a) => [a.getAttribute('href').slice(1), a]));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        for (const a of links.values()) a.classList.remove('is-active');
        links.get(e.target.id)?.classList.add('is-active');
      }
    }
  }, { rootMargin: '0px 0px -70% 0px' });
  for (const h of heads) io.observe(h);
}

/* ------------------------------------------------------------------ copy buttons */
function addCopyButtons() {
  for (const pre of $$('.prose pre')) {
    const wrap = document.createElement('div');
    wrap.className = 'code-wrap';
    pre.replaceWith(wrap);
    wrap.append(pre);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code to clipboard');
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText);
        btn.textContent = 'Copied';
      } catch {
        btn.textContent = 'Select & copy';
      }
      setTimeout(() => { btn.textContent = 'Copy'; }, 1800);
    });
    wrap.append(btn);
  }
}

/* ------------------------------------------------------------------ drawn order */
async function renderOrder() {
  const [draft, personas] = await Promise.all([loadData('draft.json'), loadData('personas.json')]);
  const league = buildLeague({ draft, personas, manifest });
  const el = $('#drawn-order');
  if (!league.order) {
    mount(el, html`<p class="notice">${ICONS.info}<span>The order is drawn ${fmtWhen('2026-09-27T18:00:00Z')} and appears here once the draft export is published.</span></p>`);
    return;
  }
  mount(el, html`<h3 id="the-drawn-order">The drawn order</h3>
    <p>Round 1 runs top to bottom; even rounds run in reverse.</p>
    <ol class="order-list">${league.order.map((id) => {
      const t = league.byId.get(id);
      return html`<li>${crest(t, { size: 'sm' })}<a href="${t.url}">${t.hasPersona ? t.franchise : t.isBot ? 'Autodraft' : t.displayModel}</a></li>`;
    })}</ol>`);
}

/* ------------------------------------------------------------------ ledger */
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

async function verify(path, status) {
  if (!crypto?.subtle) {
    status.textContent = 'Your browser can’t run SHA-256 here (it needs a secure https page). Use the Python check above.';
    return;
  }
  status.className = 'verify-status';
  status.textContent = 'Downloading the ledger…';
  const text = await fetchText(path);
  const lines = text.split('\n').map((l) => l.replace(/\r$/, '')).filter((l) => l.trim());
  const enc = new TextEncoder();
  let prev = '0'.repeat(64);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let ev;
    try {
      ev = JSON.parse(line);
    } catch {
      return { ok: false, seq: i + 1, reason: 'line is not valid JSON' };
    }
    if (ev.seq !== i + 1) return { ok: false, seq: ev.seq, reason: `expected event #${i + 1}` };
    if (ev.prev_hash !== prev) return { ok: false, seq: ev.seq, reason: 'prev_hash doesn’t match the previous event' };
    // Lines are written as canonical JSON, so removing the "hash" member leaves the exact bytes that were hashed.
    const member = `"hash":"${ev.hash}"`;
    const body = line.includes(`,${member}`) ? line.replace(`,${member}`, '') : line.replace(`${member},`, '');
    if (hex(await crypto.subtle.digest('SHA-256', enc.encode(body))) !== ev.hash) {
      return { ok: false, seq: ev.seq, reason: 'hash doesn’t match the event contents' };
    }
    prev = ev.hash;
    if (i % 250 === 0) status.textContent = `Checking event ${i + 1} of ${lines.length}…`;
  }
  return { ok: true, count: lines.length, head: prev };
}

function renderLedger() {
  const box = $('#verify-box');
  const head = manifest?.ledger_head;
  const pub = manifest?.ledger?.path;
  mount(box, html`
    ${head ? html`<p class="verify-status">Latest ledger head: <strong>event #${head.seq}</strong>${head.ts ? html` · ${fmtWhen(head.ts)}` : ''}</p>
      <p class="head-hash" style="margin:0">${head.hash}</p>` : html`<p class="verify-status">The current ledger head appears here after the next sync.</p>`}
    ${pub ? html`<div class="btn-row"><button class="btn btn--navy btn--sm" type="button" id="verify-btn">Verify in your browser</button>
      <a class="btn btn--outline btn--sm" href="${pub}" download="league.jsonl">Download league.jsonl</a></div>
      <p class="verify-status" id="verify-status" role="status" aria-live="polite"></p>` : ''}`);
  const btn = $('#verify-btn');
  btn?.addEventListener('click', async () => {
    const status = $('#verify-status');
    btn.disabled = true;
    try {
      const r = await verify(pub, status);
      if (!r) return;
      status.className = `verify-status ${r.ok ? 'ok' : 'bad'}`;
      status.textContent = r.ok
        ? `✓ All ${r.count} events verified: every link and every hash matches. Head ${r.head.slice(0, 16)}…`
        : `✗ Chain broken at event #${r.seq}: ${r.reason}.`;
    } catch (err) {
      status.className = 'verify-status bad';
      status.textContent = `Couldn’t check the ledger (${err.message}).`;
    } finally {
      btn.disabled = false;
    }
  });
}

addCopyButtons();
renderLedger();
await renderOrder();
buildToc();
