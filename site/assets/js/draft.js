// Draft board: polls data/draft.json (cache-busted) every ~15 s while the draft runs.
import './chrome.js';
import {
  CONFIG, $, $$, html, raw, spoken, mount, loadManifest, loadData, fetchJSON, buildLeague, snakeSlots, crest, splitName,
  POS, POS_SHORT, groupOf, fmtWhen, fmtClock, ago, ICONS, now, buildReport,
} from './lib.js';
import { reportCard, delusionIndex } from './report.js';

const manifest = await loadManifest();
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

const S = {
  league: null,
  slots: [],
  picks: new Map(),      // pick_no -> pick
  reactions: new Map(),  // pick_no -> says
  talk: new Map(),       // round -> table talk says
  sig: null,             // last rendered data signature
  nextNo: 1,
  complete: false,
  hasDraftFile: !manifest || !!manifest.files?.['draft.json'],
  lastOk: null,
  failures: 0,
  timer: null,
  filter: new URLSearchParams(location.search).get('team') || '',
  view: 'grid',
  openNo: null,
  opener: null,
  roundOpen: new Map(),  // round -> user-chosen open state in the list view
  drawerKey: '',         // what the open drawer last showed (reactions/talk counts)
};

const el = {
  pill: $('#status-pill'), fill: $('#progress-fill'), ptext: $('#progress-text'), psub: $('#progress-sub'),
  kicker: $('#draft-kicker'), sync: $('#sync-text'), dot: $('.sync-dot'),
  ticker: $('#ticker'), track: $('#ticker-track'), otc: $('#otc'), upnext: $('#upnext'),
  board: $('#board'), list: $('#pick-list'), wrap: $('#board-wrap'), filter: $('#team-filter'),
  talk: $('#talk'), talkBody: $('#talk-body'), announcer: $('#announcer'), ledgerNote: $('#ledger-note'),
  dialog: $('#pick-dialog'), pdHead: $('#pd-head'), pdKicker: $('#pd-kicker'), pdTitle: $('#pd-title'),
  pdSub: $('#pd-sub'), pdBody: $('#pd-body'), pdPrev: $('#pd-prev'), pdNext: $('#pd-next'),
};

const team = (id) => S.league?.byId.get(id) || { id, abbrev: String(id || '?').slice(0, 3).toUpperCase(), gmShort: id, gmLabel: id, franchise: id, displayModel: id, style: '', url: '#', initials: '?' };
const teamName = (t) => (t.hasPersona ? t.franchise : t.isBot ? 'Autodraft' : t.displayModel);
const autoKind = (p) => (!p?.auto ? null : p.auto.reason === 'control_bot' ? 'bot' : 'auto');
const started = () => now() >= Date.parse(CONFIG.draftStart);

/* ------------------------------------------------------------------ data */
async function load() {
  if (!S.hasDraftFile) {
    // Not published yet: watch the (tiny) manifest until a sync lists draft.json.
    const m = await fetchJSON('data/manifest.json', { bust: true }).catch(() => null);
    if (!m || m.files?.['draft.json']) S.hasDraftFile = true;
    else return null;
  }
  return fetchJSON('data/draft.json', { bust: true });
}

function ingest(draft) {
  const league = buildLeague({ draft, manifest });
  const order = league.order;
  S.league = league;
  S.slots = order ? snakeSlots(order, league.rounds) : [];
  S.picks = new Map(league.picks.map((p) => [Number(p.pick_no), p]));
  S.reactions = new Map();
  S.talk = new Map();
  for (const s of league.says) {
    if (s.kind === 'table_talk') {
      if (!S.talk.has(s.round)) S.talk.set(s.round, []);
      S.talk.get(s.round).push(s);
    } else if (s.pick_no != null) {
      const k = Number(s.pick_no);
      if (!S.reactions.has(k)) S.reactions.set(k, []);
      S.reactions.get(k).push(s);
    }
  }
  const maxNo = Math.max(0, ...S.picks.keys());
  S.nextNo = maxNo + 1;
  S.complete = !!league.total && S.picks.size >= league.total;
}

/* ------------------------------------------------------------------ status */
function renderHead() {
  const L = S.league;
  const total = L?.total || (L?.teams.length || 14) * (L?.rounds || 14);
  const made = S.picks.size;
  el.kicker.textContent = `Draft Night · ${fmtWhen(CONFIG.draftStart)}`;
  let pill = ['pill--pre', 'Pre-draft'];
  if (S.complete) pill = ['pill--final', 'Final'];
  else if (made > 0 || (L?.order && started())) pill = ['pill--live', 'Live'];
  el.pill.className = `pill ${pill[0]}`;
  el.pill.firstElementChild.textContent = pill[1];
  el.fill.style.width = `${total ? Math.min(100, (made / total) * 100) : 0}%`;

  if (!L?.order) {
    mount(el.ptext, html`<span>${L?.teams.length || 14} teams · ${L?.rounds || 14} rounds · snake order</span>`);
    el.psub.textContent = `Order drawn ${fmtWhen(CONFIG.orderReveal)}`;
  } else if (S.complete) {
    mount(el.ptext, html`<span><strong>${made} of ${total}</strong> picks · draft complete</span>`);
    el.psub.textContent = L.draft?.generated_at ? `Final pick logged ${fmtWhen(L.draft.generated_at)}` : '';
  } else if (made === 0) {
    mount(el.ptext, html`<span><strong>0 of ${total}</strong> picks</span>`);
    el.psub.textContent = started() ? 'Waiting on pick #1' : `Starts ${fmtWhen(CONFIG.draftStart)}`;
  } else {
    const slot = S.slots[S.nextNo - 1];
    mount(el.ptext, html`<span><strong>${made} of ${total}</strong> picks in${slot ? ` · Round ${slot.round}` : ''}</span>`);
    el.psub.textContent = L.draft?.generated_at ? `Last pick ${ago(L.draft.generated_at)}` : '';
  }
}

function renderSync(state) {
  el.dot.classList.toggle('is-stale', state === 'error');
  el.dot.classList.toggle('is-off', state === 'idle');
  const seq = S.league?.draft?.ledger_head_seq;
  const seqText = seq ? ` · ledger seq ${seq}` : '';
  if (state === 'error') el.sync.textContent = `Connection hiccup — retrying${S.lastOk ? ` (last update ${fmtClock(S.lastOk)})` : ''}`;
  else if (state === 'nodata') el.sync.textContent = 'The board goes live once the draft order is published.';
  else if (S.complete) el.sync.textContent = `Final board${seqText}`;
  else {
    const every = `checks every ${Math.round(CONFIG.pollMs / 1000)} s`;
    const at = `updated ${fmtClock(S.lastOk || new Date())}`;
    if (!S.league?.order) el.sync.textContent = `Waiting for the draft order · ${at} · ${every}`;
    else if (!S.picks.size) el.sync.textContent = `Board set, waiting for pick #1 · ${at} · ${every}`;
    else el.sync.textContent = `Live · ${at} · ${every}${seqText}`;
  }
  if (seq) mount(el.ledgerNote, html`Every pick is an event on the league's hash-chained ledger — this board reflects it through <strong>event #${seq}</strong>. <a href="methodology.html#verify">How to verify it</a>.`);
}

/* ------------------------------------------------------------------ on the clock / up next */
function renderOTC() {
  const L = S.league;
  if (!L?.order) return; // keep the static drand explainer
  el.otc.removeAttribute('style');
  if (S.complete) {
    const last = S.picks.get(S.slots.length);
    const t = team(last?.team);
    el.otc.setAttribute('style', t.style || '');
    mount(el.otc, html`
      <div class="otc-label"><span class="pill pill--final"><span>Draft complete</span></span></div>
      <div class="otc-main">${crest(t, { size: 'lg' })}<div>
        <p class="otc-slot">Final pick · #${S.slots.length}</p>
        <h2>${last ? last.player_name : 'Draft complete'}</h2>
        <p>${last ? `${teamName(t)} · ${last.nhl_team} ${POS[last.position] || last.position}` : ''}</p>
      </div></div>
      <p>${S.slots.length} picks across ${L.rounds} rounds. Tap any pick for the GM's reasoning and the room's reaction.</p>`);
    return;
  }
  const slot = S.slots[S.nextNo - 1];
  if (!slot) return;
  const t = team(slot.team);
  const live = S.picks.size > 0 || started();
  el.otc.setAttribute('style', t.style);
  mount(el.otc, html`
    <div class="otc-label"><span class="pill ${live ? 'pill--live' : 'pill--muted'}"><span>${live ? 'On the clock' : 'First up'}</span></span>
      <span class="otc-slot">Round ${slot.round} · Pick ${slot.inRound} · #${slot.pick_no} overall</span></div>
    <div class="otc-main">${crest(t, { size: 'lg' })}<div>
      <h2><a href="${t.url}">${teamName(t)}</a></h2>
      <p>${t.hasPersona ? `GM ${t.gmShort} · ` : ''}${t.displayModel}${t.lab ? ` · ${t.lab}` : ''}</p>
    </div></div>
    <p>${t.isBot ? 'The control bot takes the best available player by the house projection.' : 'No pick clock: the GM researches with the league tools, then files its pick with a public statement.'}</p>`);
}

function renderRecap() {
  const picks = [...S.picks.values()].sort((a, b) => a.pick_no - b.pick_no);
  const first = (g) => picks.find((p) => groupOf(p.position, p.group) === g);
  const counts = { F: 0, D: 0, G: 0 };
  for (const p of picks) counts[groupOf(p.position, p.group)] += 1;
  const autos = picks.filter((p) => autoKind(p) === 'auto').length;
  const reactions = [...S.reactions.values()].reduce((n, a) => n + a.length, 0);
  const talk = [...S.talk.values()].reduce((n, a) => n + a.length, 0);
  const firstLine = (p, label) => (p ? html`<div><dt>${label}</dt><dd><button type="button" class="recap-link" data-pick="${p.pick_no}">#${p.pick_no} ${p.player_name}</button> <span class="meta">${team(p.team).abbrev}</span></dd></div>` : '');
  mount(el.upnext, html`<h2>By the numbers</h2>
    <dl class="recap">
      <div><dt>Forwards · Defence · Goalies</dt><dd><strong>${counts.F} · ${counts.D} · ${counts.G}</strong></dd></div>
      ${firstLine(first('D'), 'First defenceman')}
      ${firstLine(first('G'), 'First goalie')}
      <div><dt>Autopicks</dt><dd><strong>${autos}</strong> <span class="meta">${autos ? 'logged on the ledger' : 'every GM filed its own picks'}</span></dd></div>
      <div><dt>On-air lines</dt><dd><strong>${reactions + talk}</strong> <span class="meta">${reactions} reactions · ${talk} table talk</span></dd></div>
      ${RC.report ? html`<div><dt>Report card</dt><dd><a class="recap-link" href="#report-card">Top grade: ${team(RC.report.rows[0].team).abbrev}</a></dd></div>` : ''}
    </dl>`);
}

function renderField() {
  const teams = [...(S.league?.teams || [])].sort((a, b) => teamName(a).localeCompare(teamName(b)));
  if (!teams.length) return;
  mount(el.upnext, html`<h2>The field</h2><ol class="field-list">${teams.map((t) => html`<li>
    <a class="team-line team-line--link" href="${t.url}">${crest(t, { size: 'xs' })}<span class="team-line-text"><strong>${teamName(t)}</strong><span>${t.displayModel}</span></span></a></li>`)}</ol>`);
}

function renderUpNext() {
  if (!S.league?.order) {
    renderField();
    return;
  }
  if (S.complete) {
    renderRecap();
    return;
  }
  const start = S.complete ? S.slots.length : S.nextNo;
  const next = S.slots.slice(start, start + 4);
  if (!next.length) {
    mount(el.upnext, html`<h2>Up next</h2><p class="muted-note">${S.complete ? 'That’s the draft. Rosters are on each team page.' : '—'}</p>`);
    return;
  }
  let prevTeam = S.slots[start - 1]?.team;
  mount(el.upnext, html`<h2>Up next</h2><ol class="next-list">${next.map((s) => {
    const t = team(s.team);
    const turn = s.team === prevTeam;
    prevTeam = s.team;
    return html`<li><span class="u-no">#${s.pick_no}</span>
      <a class="team-line team-line--link" href="${t.url}">${crest(t, { size: 'sm' })}<span class="team-line-text"><strong>${teamName(t)}</strong><span>${t.hasPersona ? t.gmShort + ' · ' : ''}${t.displayModel}</span></span></a>
      <span class="u-turn">${turn ? 'Back-to-back' : `Rd ${s.round}`}</span></li>`;
  })}</ol>`);
}

/* ------------------------------------------------------------------ board */
function ariaPick(p, t) {
  const kind = autoKind(p);
  return `Pick ${p.pick_no}, round ${p.round}: ${teamName(t)} select ${p.player_name}, ${POS[p.position] || p.position}, ${p.nhl_team}.${kind === 'auto' ? ' Autopick.' : ''}`;
}

function flags(p) {
  const k = autoKind(p);
  if (k === 'auto') return html`<span class="tag tag--auto" title="Autopick">Auto</span>`;
  if (k === 'bot') return html`<span class="tag tag--bot" title="Control bot pick">Bot</span>`;
  return '';
}

function cell(slot, fresh) {
  const p = S.picks.get(slot.pick_no);
  const t = team(slot.team);
  if (p) {
    const g = groupOf(p.position, p.group);
    const [first, last] = splitName(p.player_name);
    return html`<td data-col="${slot.col}"><button type="button" class="pk g-${g}${fresh.has(slot.pick_no) ? ' is-new' : ''}" data-pick="${slot.pick_no}" aria-label="${ariaPick(p, t)}" title="${p.player_name} (${p.nhl_team} ${POS_SHORT[p.position] || p.position})">
      <span class="pk-top"><span class="pk-no">#${slot.pick_no}</span><span class="pk-flags">${flags(p)}<span class="tag tag--${g}">${POS_SHORT[p.position] || p.position}</span></span></span>
      <span class="pk-first">${first}</span><span class="pk-last">${last}</span><span class="pk-meta">${p.nhl_team}</span>
    </button></td>`;
  }
  if (!S.complete && slot.pick_no === S.nextNo) {
    return html`<td data-col="${slot.col}"><div class="pk pk--otc"><span class="pk-otc-label">${S.picks.size || started() ? 'On the clock' : 'First up'}</span><span class="pk-otc-team">${t.abbrev}</span><span class="pk-meta">#${slot.pick_no}</span></div></td>`;
  }
  return html`<td data-col="${slot.col}"><div class="pk pk--empty"><span class="pk-top"><span class="pk-no">#${slot.pick_no}</span></span></div></td>`;
}

function renderBoard(fresh) {
  const L = S.league;
  if (!L?.order) {
    mount(el.board, html`<tbody><tr><td style="padding:24px">
      <div class="empty"><h3>The board fills in once the order is drawn</h3>
      <p>drand round ${CONFIG.drandRound} publishes at ${fmtWhen(CONFIG.orderReveal)}. The first team picks first and the order snakes every round.</p></div>
    </td></tr></tbody>`);
    return;
  }
  const n = L.order.length;
  const head = html`<thead><tr><th scope="col" class="b-corner">Rd</th>${L.order.map((id, i) => {
    const t = team(id);
    return html`<th scope="col" data-col="${i}"><a class="b-team" href="${t.url}" style="${t.style}" title="${teamName(t)} — ${t.gmName} (${t.displayModel})">
      <span class="b-top"><span class="b-abbr">${t.abbrev}</span><span class="b-slot" aria-hidden="true">${i + 1}</span></span>
      <span class="b-gm">${t.gmLabel}</span><span class="b-model">${t.displayModel}</span></a></th>`;
  })}</tr></thead>`;
  const rows = [];
  for (let r = 1; r <= L.rounds; r++) {
    const inRound = S.slots.slice((r - 1) * n, r * n).sort((a, b) => a.col - b.col);
    rows.push(html`<tr><th scope="row"><div class="b-round"><strong aria-hidden="true">${r}</strong><span aria-hidden="true">${r % 2 ? '→' : '←'}</span><span class="sr-only">Round ${r}</span></div></th>${inRound.map((s) => cell(s, fresh))}</tr>`);
  }
  mount(el.board, html`${head}<tbody>${rows}</tbody>`);
  applyFilter();
}

/* ------------------------------------------------------------------ list (phones) */
function listRow(slot) {
  const p = S.picks.get(slot.pick_no);
  const t = team(slot.team);
  if (p) {
    const g = groupOf(p.position, p.group);
    return html`<li data-team="${t.id}"><button type="button" class="pl-row g-${g}" data-pick="${slot.pick_no}" aria-label="${ariaPick(p, t)}">
      <span class="pl-no">${slot.pick_no}</span><span class="pl-abbr" style="${t.style}">${t.abbrev}</span>
      <span class="pl-main"><span class="pl-name">${p.player_name}</span><span class="pl-sub">${p.nhl_team} · ${POS[p.position] || p.position} · ${t.gmLabel}</span>${(p.on_air_call || p.public_rationale) ? html`<span class="pl-why">“${spoken(p.on_air_call || p.public_rationale)}”</span>` : ''}</span>
      <span class="pl-right">${flags(p)}<span class="tag tag--${g}">${POS_SHORT[p.position] || p.position}</span></span>
    </button></li>`;
  }
  const otc = !S.complete && slot.pick_no === S.nextNo;
  return html`<li data-team="${t.id}"${otc ? raw(' id="list-otc"') : ''}><div class="pl-row ${otc ? 'pl-row--otc' : 'pl-row--empty'}">
    <span class="pl-no">${slot.pick_no}</span><span class="pl-abbr" style="${t.style}">${t.abbrev}</span>
    <span class="pl-main"><span class="pl-name">${otc ? (S.picks.size || started() ? 'On the clock' : 'First up') : teamName(t)}</span><span class="pl-sub">${otc ? `${teamName(t)} · ` : ''}${t.hasPersona ? t.gmShort + ' · ' : ''}${t.displayModel}</span></span>
    <span class="pl-right"></span></div></li>`;
}

function renderList() {
  const L = S.league;
  if (!L?.order) {
    mount(el.list, html`<div class="empty"><h3>The board fills in once the order is drawn</h3><p>${fmtWhen(CONFIG.orderReveal)}</p></div>`);
    return;
  }
  const n = L.order.length;
  const current = S.complete ? L.rounds : Math.ceil(S.nextNo / n);
  const parts = [];
  for (let r = 1; r <= L.rounds; r++) {
    const slots = S.slots.slice((r - 1) * n, r * n);
    const open = S.roundOpen.has(r) ? S.roundOpen.get(r) : r <= current;
    parts.push(html`<details class="pl-round" data-round="${r}"${open ? ' open' : ''}>
      <summary class="pl-round-head">Round ${r} <span>Picks ${slots[0].pick_no}–${slots[slots.length - 1].pick_no}</span></summary>
      <ol class="pl-items">${slots.map(listRow)}</ol></details>`);
  }
  const jump = !S.complete && S.picks.size ? html`<p><a class="link-arrow" href="#list-otc">Jump to pick #${S.nextNo}</a></p>` : '';
  mount(el.list, html`${jump}${parts}`);
  for (const d of $$('details.pl-round', el.list)) {
    d.addEventListener('toggle', () => S.roundOpen.set(Number(d.dataset.round), d.open));
  }
  applyFilter();
}

/* ------------------------------------------------------------------ filter + view */
function setupControls() {
  const L = S.league;
  const opts = (L?.teams || []).map((t) => html`<option value="${t.id}">${teamName(t)} (${t.abbrev})</option>`);
  mount(el.filter, html`<option value="">All teams</option>${opts}`);
  if (S.filter && !L?.byId.has(S.filter)) S.filter = '';
  el.filter.value = S.filter;
}

function applyFilter() {
  const f = S.filter;
  const col = f && S.league?.order ? S.league.order.indexOf(f) : -1;
  el.board.classList.toggle('is-filtered', col >= 0);
  for (const c of $$('[data-col]', el.board)) c.classList.toggle('is-focus', col >= 0 && Number(c.dataset.col) === col);
  for (const li of $$('li[data-team]', el.list)) li.hidden = !!f && li.dataset.team !== f;
}

el.filter.addEventListener('change', () => {
  S.filter = el.filter.value;
  const url = new URL(location.href);
  if (S.filter) url.searchParams.set('team', S.filter);
  else url.searchParams.delete('team');
  history.replaceState(null, '', url);
  applyFilter();
});

function setView(v) {
  S.view = v === 'list' ? 'list' : 'grid';
  el.wrap.dataset.view = S.view;
  for (const b of $$('[data-view-btn]')) b.setAttribute('aria-pressed', String(b.dataset.viewBtn === S.view));
  try { localStorage.setItem('suits.draftView', S.view); } catch { /* storage unavailable */ }
}
for (const b of $$('[data-view-btn]')) b.addEventListener('click', () => setView(b.dataset.viewBtn));
try { setView(localStorage.getItem('suits.draftView') || 'grid'); } catch { setView('grid'); }

/* ------------------------------------------------------------------ ticker */
function renderTicker() {
  const made = [...S.picks.values()].sort((a, b) => b.pick_no - a.pick_no).slice(0, 12);
  el.ticker.hidden = !made.length;
  el.ticker.classList.remove('is-moving');
  if (!made.length) return;
  const items = made.map((p, i) => {
    const t = team(p.team);
    return html`<button type="button" class="ticker-item" data-pick="${p.pick_no}" style="${t.style}">
      ${i === 0 && !S.complete ? html`<span class="t-new">Just in</span>` : ''}<span class="t-no">#${p.pick_no}</span><span class="t-abbr">${t.abbrev}</span><strong>${p.player_name}</strong><span>${p.nhl_team} ${POS_SHORT[p.position] || p.position}</span>${flags(p)}</button>`;
  });
  mount(el.track, html`<span class="ticker-copy">${items}</span>`);
  requestAnimationFrame(() => {
    const vp = el.track.parentElement;
    const w = el.track.scrollWidth;
    if (reduceMotion.matches || w <= vp.clientWidth) return;
    const copy = el.track.firstElementChild.cloneNode(true);
    copy.setAttribute('aria-hidden', 'true');
    for (const b of copy.querySelectorAll('button')) b.tabIndex = -1;
    el.track.append(copy);
    el.ticker.style.setProperty('--ticker-dur', `${Math.max(20, Math.round(w / 45))}s`);
    el.ticker.classList.add('is-moving');
  });
}

/* ------------------------------------------------------------------ table talk */
function sayItem(s, { showTarget = false } = {}) {
  const t = team(s.team);
  const target = showTarget && s.addressed_to ? team(s.addressed_to) : null;
  return html`<li class="say">${crest(t, { size: 'sm' })}<div class="say-bubble">
    <div class="say-who"><strong>${t.hasPersona ? t.gmShort : teamName(t)}</strong><span>${t.hasPersona ? t.franchise : t.lab}</span>${target ? html`<span>→ ${target.abbrev}</span>` : ''}</div>
    <p class="say-line">${spoken(s.line)}</p></div></li>`;
}

function renderTalk() {
  const rounds = [...S.talk.keys()].sort((a, b) => b - a);
  el.talk.hidden = !rounds.length;
  if (!rounds.length) return;
  const block = (r) => html`<div class="talk-round"><h3>After round ${r}</h3><ul class="says">${S.talk.get(r).map((s) => sayItem(s))}</ul></div>`;
  const recent = rounds.slice(0, 2);
  const older = rounds.slice(2);
  mount(el.talkBody, html`<div class="talk-grid">${recent.map(block)}</div>
    ${older.length ? html`<details class="talk-more"><summary>Earlier rounds (${older.length})</summary><div class="talk-grid">${older.map(block)}</div></details>` : ''}`);
}

/* ------------------------------------------------------------------ drawer */
function projection(p) {
  const pts = p.projected_points == null || p.projected_points === '' ? NaN : Number(p.projected_points);
  const range = Array.isArray(p.range_80) && p.range_80.length === 2 && p.range_80.every((v) => v != null && Number.isFinite(Number(v)))
    ? p.range_80.map(Number) : null;
  if (!Number.isFinite(pts) && !range) {
    return html`<p class="muted-note">${autoKind(p) === 'bot' ? 'The control bot doesn’t file projections.' : 'No projection filed with this pick.'}</p>`;
  }
  const hi = Math.max(pts || 0, range ? range[1] : 0);
  const max = Math.max(60, Math.ceil((hi * 1.15) / 20) * 20);
  const pct = (v) => `${Math.max(0, Math.min(100, (v / max) * 100))}%`;
  return html`<div class="proj">
    <div class="proj-figs">
      ${Number.isFinite(pts) ? html`<span class="proj-big">${pts}<small>pts projected</small></span>` : ''}
      ${range ? html`<span class="proj-range">80% range: <strong>${range[0]}–${range[1]}</strong></span>` : ''}
    </div>
    <div class="proj-bar" role="img" aria-label="${`Projection ${Number.isFinite(pts) ? pts : 'n/a'} points${range ? `, 80% range ${range[0]} to ${range[1]}` : ''}, on a 0 to ${max} scale`}">
      ${range ? html`<span class="r" style="${`left:${pct(range[0])};width:calc(${pct(range[1])} - ${pct(range[0])})`}"></span>` : ''}
      ${Number.isFinite(pts) ? html`<span class="p" style="${`left:${pct(pts)}`}"></span>` : ''}
    </div>
    <div class="proj-scale" aria-hidden="true"><span>0</span><span>${max / 2}</span><span>${max}</span></div>
    <p class="meta" style="margin:0">The GM's own projection of his 2026-27 fantasy points, filed with the pick.</p>
  </div>`;
}

function autoNote(p) {
  const k = autoKind(p);
  if (!k) return '';
  if (k === 'bot') {
    return html`<div class="flag-note flag-note--bot">${ICONS.info}<p>Control bot pick. Autodraft takes the best available player by the house projection — the baseline every AI GM is measured against.</p></div>`;
  }
  const reason = String(p.auto.reason || 'unreachable').replace(/_/g, ' ');
  const source = p.auto.source === 'queue' ? 'the top legal player on its own draft queue' : 'the best available player by the house projection';
  return html`<div class="flag-note">${ICONS.info}<p><strong>Autopick.</strong> The GM couldn't complete this pick (${reason}), so the league took ${source}. Logged as such on the ledger.</p></div>`;
}

function renderDrawer(no) {
  const p = S.picks.get(no);
  const slot = S.slots[no - 1];
  if (!p || !slot) return false;
  const t = team(p.team);
  el.pdHead.setAttribute('style', t.style);
  el.pdKicker.textContent = `Round ${slot.round} · Pick ${slot.inRound} · #${no} overall`;
  el.pdTitle.textContent = p.player_name;
  el.pdSub.textContent = `${p.nhl_team} · ${POS[p.position] || p.position}`;
  const reactions = S.reactions.get(no) || [];
  const talk = S.talk.get(slot.round) || [];
  S.drawerKey = drawerKey(no);
  mount(el.pdBody, html`
    <a class="team-line team-line--link" href="${t.url}">${crest(t, { size: 'md' })}
      <span class="team-line-text"><strong>${teamName(t)}</strong><span>${t.hasPersona ? `GM ${t.gmShort} · ` : ''}${t.displayModel}${t.lab ? ` · ${t.lab}` : ''}</span></span></a>
    ${autoNote(p)}
    <section aria-labelledby="pd-why"><h3 id="pd-why">${autoKind(p) ? 'Pick note' : 'On-air call'}</h3>
      ${p.on_air_call ? html`<blockquote class="statement">${spoken(p.on_air_call)}</blockquote>` : ''}
      ${p.public_rationale ? html`${p.on_air_call ? html`<h3 class="pd-sub">The reasoning, for the record</h3>` : ''}<p class="${p.on_air_call ? 'pd-reason' : 'statement'}">${p.public_rationale}</p>` : ''}
      ${!p.on_air_call && !p.public_rationale ? html`<p class="muted-note">No statement filed.</p>` : ''}</section>
    <section aria-labelledby="pd-proj"><h3 id="pd-proj">Projection</h3>${projection(p)}</section>
    <section aria-labelledby="pd-react"><h3 id="pd-react">Reactions${reactions.length ? ` (${reactions.length})` : ''}</h3>
      ${reactions.length ? html`<ul class="says">${reactions.map((s) => sayItem(s))}</ul>` : html`<p class="muted-note">No GM was cued to react to this pick.</p>`}</section>
    <section aria-labelledby="pd-talk"><h3 id="pd-talk">Round ${slot.round} table talk</h3>
      ${talk.length ? html`<ul class="says">${talk.map((s) => sayItem(s))}</ul>` : html`<p class="muted-note">${slot.round * (S.league.order.length) < S.nextNo ? 'No table talk was logged for this round.' : 'Table talk happens at the round break.'}</p>`}</section>`);
  updateDrawerNav(no);
  return true;
}

function updateDrawerNav(no) {
  const made = [...S.picks.keys()].sort((a, b) => a - b);
  const i = made.indexOf(no);
  el.pdPrev.disabled = i <= 0;
  el.pdNext.disabled = i < 0 || i >= made.length - 1;
  el.pdPrev.dataset.to = made[i - 1] ?? '';
  el.pdNext.dataset.to = made[i + 1] ?? '';
}

function drawerKey(no) {
  const slot = S.slots[no - 1];
  return `${no}|${(S.reactions.get(no) || []).length}|${slot ? (S.talk.get(slot.round) || []).length : 0}`;
}

function openPick(no, opener) {
  if (!renderDrawer(no)) return;
  S.openNo = no;
  if (opener) S.opener = opener;
  if (!el.dialog.open) el.dialog.showModal();
  el.pdBody.scrollTop = 0;
  history.replaceState(null, '', `${location.pathname}${location.search}#pick-${no}`);
}

el.dialog.addEventListener('close', () => {
  S.openNo = null;
  history.replaceState(null, '', `${location.pathname}${location.search}`);
  const back = S.opener && document.contains(S.opener) ? S.opener : $(`[data-pick="${S.opener?.dataset?.pick}"]`);
  back?.focus?.();
});
$('#pd-close').addEventListener('click', () => el.dialog.close());
el.dialog.addEventListener('click', (e) => {
  if (e.target !== el.dialog) return;
  const r = el.dialog.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) el.dialog.close();
});
for (const b of [el.pdPrev, el.pdNext]) b.addEventListener('click', () => b.dataset.to && openPick(Number(b.dataset.to)));
el.dialog.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea, select')) return;
  if (e.key === 'ArrowLeft' && !el.pdPrev.disabled) { e.preventDefault(); el.pdPrev.click(); }
  if (e.key === 'ArrowRight' && !el.pdNext.disabled) { e.preventDefault(); el.pdNext.click(); }
});
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-pick]');
  if (b && !el.dialog.contains(b)) openPick(Number(b.dataset.pick), b);
});

/* ------------------------------------------------------------------ keyboard: arrow keys move between board cells */
el.board.addEventListener('keydown', (e) => {
  const moves = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };
  const btn = e.target.closest('button.pk');
  if (!btn || !moves[e.key]) return;
  const td = btn.closest('td');
  const tr = td.parentElement;
  const rows = [...el.board.tBodies[0].rows];
  let r = rows.indexOf(tr);
  let c = [...tr.cells].indexOf(td);
  const [dr, dc] = moves[e.key];
  for (;;) {
    r += dr;
    c += dc;
    const cellEl = rows[r]?.cells[c];
    if (!cellEl || c < 1) return;
    const next = cellEl.querySelector('button.pk');
    if (next) {
      e.preventDefault();
      next.focus();
      next.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      return;
    }
  }
});

/* ------------------------------------------------------------------ announcements */
function announce(fresh) {
  if (!fresh.length) return;
  const lines = fresh.slice(-3).map((no) => {
    const p = S.picks.get(no);
    return p ? ariaPick(p, team(p.team)) : '';
  });
  el.announcer.textContent = fresh.length > 3 ? `${fresh.length} new picks. Latest: ${lines.join(' ')}` : lines.join(' ');
}

/* ------------------------------------------------------------------ refresh loop */
function signature(d) {
  return d ? `${d.ledger_head_seq}|${(d.picks || []).length}|${(d.says || []).length}|${(d.order || []).join(',')}|${d.generated_at}` : 'none';
}

async function refresh(initial = false) {
  let draft;
  try {
    draft = await load();
  } catch {
    S.failures += 1;
    if (initial && !S.league) renderEmpty();
    renderSync('error');
    return;
  }
  S.failures = 0;
  S.lastOk = new Date();
  if (!draft) {
    if (initial) {
      // Nothing drafted yet: introduce the field from the Media Day personas, if published.
      const personas = await loadData('personas.json');
      if (personas) S.league = buildLeague({ personas, manifest });
      renderEmpty();
      renderField();
    }
    renderSync('nodata');
    return;
  }
  const sig = signature(draft);
  if (sig !== S.sig) {
    const before = new Set(S.picks.keys());
    const focused = document.activeElement?.closest?.('[data-pick]')?.dataset.pick;
    const scope = document.activeElement?.closest?.('#ticker, #board, #pick-list, #upnext')?.id;
    const orderBefore = S.league?.order?.join(',');
    ingest(draft);
    const fresh = initial ? [] : [...S.picks.keys()].filter((k) => !before.has(k)).sort((a, b) => a - b);
    const freshSet = new Set(reduceMotion.matches ? [] : fresh);
    if (initial || orderBefore !== S.league.order?.join(',')) setupControls();
    renderHead();
    renderOTC();
    renderUpNext();
    renderBoard(freshSet);
    renderList();
    renderTicker();
    renderTalk();
    announce(fresh);
    if (focused && scope && !el.dialog.open) $(`#${scope} [data-pick="${focused}"]`)?.focus();
    if (el.dialog.open && S.openNo) {
      if (drawerKey(S.openNo) !== S.drawerKey) renderDrawer(S.openNo);
      else updateDrawerNav(S.openNo);
    }
    S.sig = sig;
  } else {
    renderHead();
  }
  renderSync('ok');
}

function renderEmpty() {
  renderHead();
  renderBoard(new Set());
  renderList();
}

/* ------------------------------------------------------------------ report card (post-draft peer grades) */
const RC = { section: $('#report-card'), list: $('#rc-list'), delusion: $('#rc-delusion'), report: null, timer: null };

function renderReport(doc) {
  const report = buildReport(doc);
  if (!report) return false;
  RC.report = report;
  mount(RC.list, reportCard(report, team));
  mount(RC.delusion, delusionIndex(report, team));
  RC.section.hidden = false;
  if (S.complete) renderUpNext();
  return true;
}

async function checkForGrades() {
  if (document.hidden || !S.complete) return;
  const m = await fetchJSON('data/manifest.json', { bust: true }).catch(() => null);
  if (!m?.files?.['grades.json']) return;
  const doc = await fetchJSON('data/grades.json', { bust: true }).catch(() => null);
  if (renderReport(doc)) clearInterval(RC.timer);
}

function schedule() {
  clearTimeout(S.timer);
  if (S.complete || document.hidden) return;
  const base = S.league?.order || S.hasDraftFile ? CONFIG.pollMs : 60000;
  const delay = S.failures ? Math.min(60000, base * 2 ** Math.min(S.failures, 2)) : base;
  S.timer = setTimeout(async () => {
    await refresh();
    schedule();
  }, delay);
}

document.addEventListener('visibilitychange', async () => {
  if (document.hidden) {
    clearTimeout(S.timer);
  } else if (!S.complete) {
    await refresh();
    schedule();
  }
});
let wasStarted = started();
setInterval(() => {
  if (!S.league) return;
  renderHead();
  if (started() !== wasStarted) {
    wasStarted = started();
    renderOTC();
    renderBoard(new Set());
    renderList();
  }
}, 30000);

await refresh(true);
schedule();
// grades.json lands after the post-draft session; until then check once a minute (only once the draft is over).
if (!renderReport(await loadData('grades.json'))) RC.timer = setInterval(checkForGrades, 60000);
function openFromHash() {
  const m = location.hash.match(/^#pick-(\d+)$/);
  if (m && S.picks.has(Number(m[1]))) openPick(Number(m[1]));
}
window.addEventListener('hashchange', openFromHash);
openFromHash();
