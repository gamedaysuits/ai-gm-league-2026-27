// Team detail: team.html?id=<team> (or team.html#<team>).
import './chrome.js';
import {
  CONFIG, $, html, spoken, mount, loadManifest, loadData, buildLeague, crest, swatchAttrs, humanize, validHex,
  POS, POS_SHORT, GROUP, groupOf, fmtWhen, ICONS, plural, buildReport, ordinal, clubName,
} from './lib.js';
import { gradeBadge, spreadLine, quoteList } from './report.js';

const params = new URLSearchParams(location.search);
const hashId = decodeURIComponent(location.hash.slice(1));
const manifest = await loadManifest();
const [personas, draft, fabrics, scorecard, grades] = await Promise.all([
  loadData('personas.json'), loadData('draft.json'), loadData('fabrics.json'), loadData('scorecard.json'), loadData('grades.json'),
]);
const league = buildLeague({ personas, draft, fabrics, manifest });
const report = buildReport(grades);
const lookup = (tid) => league.byId.get(tid)
  || { id: tid, abbrev: String(tid || '?').slice(0, 3).toUpperCase(), gmShort: tid, displayModel: tid, style: '', url: '#', initials: '?' };
const id = params.get('id') || (league.byId.has(hashId) ? hashId : '');
const root = $('#team-root');

const SPEC_LABELS = {
  button_layout: 'Buttons', lapel_style: 'Lapel', lapel_width: 'Lapel width', lining: 'Lining', lining_color: 'Lining colour',
  pocket_style: 'Pockets', venting: 'Vents', cuffs: 'Hem', fastening: 'Waistband', pleats: 'Front', back_style: 'Back', hem_style: 'Hem',
};
const SPEC_ORDER = Object.keys(SPEC_LABELS);
const label = (k) => SPEC_LABELS[k] || humanize(k.replace(/_/g, '-'));
const value = (k, v) => (k === 'lining_color' ? String(v) : humanize(v));
const teamName = (t) => (t.hasPersona ? t.franchise : t.isBot ? 'Autodraft' : t.displayModel);

function specGroup(title, obj) {
  if (!obj || typeof obj !== 'object') return html`<div class="spec-group"><h4>${title}</h4><p class="muted-note">None</p></div>`;
  const keys = [...SPEC_ORDER.filter((k) => k in obj), ...Object.keys(obj).filter((k) => !SPEC_ORDER.includes(k))];
  const rows = keys.filter((k) => obj[k] != null && obj[k] !== '' && typeof obj[k] !== 'object');
  return html`<div class="spec-group"><h4>${title}</h4><dl>${rows.map((k) => html`<div><dt>${label(k)}</dt><dd>${value(k, obj[k])}</dd></div>`)}</dl></div>`;
}

function suitSection(t) {
  const suit = t.persona?.suit;
  if (!suit) return '';
  const f = t.fabric;
  const sw = swatchAttrs(f, t.swatch);
  const facts = f ? [f.color_name, f.pattern_desc, f.composition, f.weight, f.collection].filter(Boolean) : [];
  const finishing = { shirt: suit.shirt, tie: suit.tie, pocket_square: suit.pocket_square };
  return html`<section class="section section--tight" id="suit" aria-labelledby="suit-title">
    <div class="container">
      <div class="section-head">
        <div><p class="kicker">Presented by Game Day Suits</p><h2 class="h-section" id="suit-title">The Game Day Suit</h2></div>
        <p>Specced by ${t.gmShort} at Media Day, cut from a real Game Day Suits fabric.</p>
      </div>
      <div class="card suit">
        <div class="suit-grid">
          <div class="suit-visual">
            <span class="${sw.cls}" style="${sw.style}" role="img" aria-label="${f ? `Fabric swatch: ${f.color_name || ''} ${f.pattern_desc || ''}`.trim() : 'Fabric swatch'}"></span>
            <div class="suit-visual-text">
              <p class="kicker">The fabric</p>
              <h3>${f ? f.name || f.id : suit.fabric_id || 'Fabric on file'}</h3>
              ${f?.summary ? html`<p>${f.summary}</p>` : ''}
              ${facts.length ? html`<ul class="fabric-facts">${facts.map((x) => html`<li>${x}</li>`)}</ul>` : ''}
            </div>
          </div>
          <div class="suit-specs">
            <div class="spec-groups">
              ${specGroup('Jacket', suit.jacket)}
              ${specGroup('Trousers', suit.pants)}
              ${suit.vest ? specGroup('Waistcoat', suit.vest) : ''}
              <div class="spec-group"><h4>Finishing</h4><dl>${Object.entries(finishing).filter(([, v]) => v).map(([k, v]) => html`<div><dt>${humanize(k.replace('_', '-'))}</dt><dd>${v}</dd></div>`)}</dl></div>
            </div>
            ${suit.rationale ? html`<blockquote class="rationale"><p style="margin:0">${suit.rationale}</p><footer>— ${t.gmShort}, on the suit</footer></blockquote>` : ''}
            <div class="suit-cta">
              <a class="btn btn--primary" href="${CONFIG.sponsorUrl}" target="_blank" rel="noopener">Build a suit like this<span class="sr-only"> at gamedaysuits.ca (opens in a new tab)</span> ${ICONS.external}</a>
              <p>Custom suits for hockey's game-day walk, made by Game Day Suits.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>`;
}

function rosterSection(t) {
  const picks = (league.picks || []).filter((p) => p.team === t.id).sort((a, b) => a.pick_no - b.pick_no);
  const byPlayer = new Map(picks.map((p) => [p.player_id, p]));
  const roster = t.roster.length ? t.roster : picks.map((p) => ({ player_id: p.player_id, name: p.player_name, nhl_team: p.nhl_team, position: p.position }));
  const head = html`<div class="section-head">
    <div><p class="kicker">Draft Night</p><h2 class="h-section" id="roster-title">Roster</h2></div>
    ${league.order ? html`<a class="link-arrow" href="draft.html?team=${encodeURIComponent(t.id)}">Follow on the draft board</a>` : ''}
  </div>`;
  if (!roster.length) {
    return html`<section class="section section--tight" id="roster" aria-labelledby="roster-title"><div class="container">${head}
      <div class="empty"><h3>The roster fills in on Draft Night</h3><p>${fmtWhen(CONFIG.draftStart)}: 14 rounds, 14 players — 6 F, 4 D and 2 G in the lineup plus 2 bench.</p></div></div></section>`;
  }
  const groups = { F: [], D: [], G: [] };
  for (const r of roster) groups[groupOf(r.position, byPlayer.get(r.player_id)?.group)].push(r);
  const item = (r) => {
    const p = byPlayer.get(r.player_id);
    const auto = p?.auto && p.auto.reason !== 'control_bot';
    return html`<li class="roster-item">
      <strong>${p ? html`<a href="draft.html#pick-${p.pick_no}">${r.name}</a>` : r.name}</strong>
      <span class="tag tag--${groupOf(r.position, p?.group)}">${POS_SHORT[r.position] || r.position}</span>
      <span class="ri-meta">${r.nhl_team} · ${POS[r.position] || r.position}${p ? ` · Rd ${p.round}, #${p.pick_no}` : ''}${Number.isFinite(Number(p?.projected_points)) && p?.projected_points != null ? ` · proj ${p.projected_points}` : ''}${auto ? ' · autopick' : ''}</span>
    </li>`;
  };
  const statements = picks.filter((p) => p.on_air_call || p.public_rationale);
  return html`<section class="section section--tight" id="roster" aria-labelledby="roster-title"><div class="container">${head}
    <div class="roster-groups">${['F', 'D', 'G'].map((g) => html`<div class="roster-group">
      <h3><span class="tag tag--${g}">${g}</span> ${GROUP[g]} <span class="meta">(${groups[g].length})</span></h3>
      <ul class="roster-list">${groups[g].length ? groups[g].map(item) : html`<li class="muted-note">None drafted</li>`}</ul></div>`)}</div>
    ${statements.length ? html`<div class="rosters" style="margin-top:24px"><details><summary><span>Pick by pick: ${t.isBot ? 'the bot’s notes' : `${t.gmShort}'s statements`}</span><span class="meta">${plural(statements.length, 'pick')}</span></summary>
      <div class="table-scroll"><table><thead><tr><th scope="col" class="num">#</th><th scope="col">Rd</th><th scope="col">Player</th><th scope="col">Statement</th></tr></thead>
      <tbody>${statements.map((p) => html`<tr><td class="num"><a href="draft.html#pick-${p.pick_no}">${p.pick_no}</a></td><td>${p.round}</td><td style="white-space:nowrap">${p.player_name} <span class="meta">${p.nhl_team} ${POS_SHORT[p.position] || p.position}</span></td><td>${p.on_air_call ? html`<strong>“${spoken(p.on_air_call)}”</strong><br><span class="meta">${p.public_rationale || ''}</span>` : p.public_rationale}</td></tr>`)}</tbody></table></div>
    </details></div>` : ''}
  </div></section>`;
}

function telemetry(t) {
  const row = (scorecard?.gms || []).find((g) => g.team === t.id);
  if (!row || t.isBot) return '';
  const pct = (x) => (Number.isFinite(Number(x)) ? `${(Number(x) * 100).toFixed(1)}%` : '–');
  const stats = [
    ['Picks filed', row.picks ?? '–'],
    ['Autopicks', row.autopicks ?? '–'],
    ['Tool calls per pick', row.tool_calls_per_pick ?? '–'],
    ['Invalid tool calls', pct(row.invalid_tool_call_rate)],
    ['Sessions completed', row.sessions != null ? `${row.sessions_ok ?? '–'} / ${row.sessions}` : '–'],
    ['Rule violations', row.violations ?? '–'],
  ];
  return html`<div class="side-block"><h2>Front-office telemetry</h2>
    <dl class="recap">${stats.map(([k, v]) => html`<div><dt>${k}</dt><dd><strong>${v}</strong></dd></div>`)}</dl>
    <p class="meta" style="margin:8px 0 0">From the league harness logs. <a href="methodology.html#fairness">How GMs are run</a>.</p></div>`;
}

/** Post-draft peer grades: what every other GM thought of this draft, and where they have it finishing. */
function verdictSection(t) {
  const r = report?.byTeam.get(t.id);
  if (!r) return '';
  const gap = r.self != null && r.league != null ? Math.round(r.league) - r.self : null;
  const spots = (n) => `${n} ${n === 1 ? 'spot' : 'spots'}`;
  let call = '';
  if (gap != null) {
    call = html`<p class="verdict-call">Predicts itself <strong>${ordinal(r.self)}</strong>. The rest of the league says <strong>${ordinal(r.league)}</strong>.</p>
      <p class="verdict-gap verdict-gap--${gap > 0 ? 'up' : gap < 0 ? 'down' : 'even'}">${gap > 0 ? `Delusion gap: ${spots(gap)}` : gap < 0 ? `Sandbagging by ${spots(-gap)}` : 'Right where the room has it'}</p>`;
  } else if (r.league != null) {
    call = html`<p class="verdict-call">${t.isBot ? 'The control bot doesn’t make predictions.' : 'No prediction filed.'} The league has it finishing <strong>${ordinal(r.league)}</strong>.</p>`;
  }
  const pr = r.projection;
  const proj = pr && pr.projected_points != null
    ? html`<p class="meta verdict-proj">Its own call for the season: <strong>${Number(pr.projected_points).toLocaleString('en-US')} points</strong>${Array.isArray(pr.range_80) && pr.range_80.length === 2 ? ` (80% range ${Number(pr.range_80[0]).toLocaleString('en-US')}–${Number(pr.range_80[1]).toLocaleString('en-US')})` : ''}.</p>`
    : '';
  return html`<section class="section section--tight" id="verdict" aria-labelledby="verdict-title"><div class="container">
    <div class="section-head">
      <div><p class="kicker">Post-draft report card</p><h2 class="h-section" id="verdict-title">What the league thinks</h2></div>
      <a class="link-arrow" href="draft.html#report-card">Full report card</a>
    </div>
    <div class="verdict">
      <div class="card card-pad verdict-summary">
        <div class="verdict-grade">${gradeBadge(r.letter, r.gpa, { size: 'xl' })}
          <div><p class="verdict-rank">${ordinal(r.rank)} of ${report.rows.length} by GPA</p>${spreadLine(r)}</div></div>
        ${call}
        ${proj}
      </div>
      <div class="card card-pad">
        <h3 class="verdict-h">Every grade it got <span class="meta">(${r.grades.length})</span></h3>
        ${quoteList(r, lookup, { lead: r.grades.length })}
      </div>
    </div>
  </div></section>`;
}

function personaBody(t) {
  const p = t.persona;
  const rivals = (Array.isArray(p.rivals) ? p.rivals : []).map((r) => league.byId.get(r)).filter(Boolean);
  const colors = [['Primary', validHex(p.primary_color)], ['Secondary', validHex(p.secondary_color)]].filter(([, c]) => c);
  return html`<div class="container">
    ${p.catchphrase ? html`<blockquote class="catch">${spoken(p.catchphrase)}</blockquote>` : html`<div style="height:32px"></div>`}
    ${p.hometown ? html`<p class="sig-call"><span class="sig-label">Hometown</span> ${p.hometown}</p>` : ''}
    ${p.cup_pick ? html`<p class="sig-call"><span class="sig-label">Stanley Cup pick</span> ${clubName(p.cup_pick)} <span class="meta">(scored in June)</span></p>` : ''}
    ${p.signature_call ? html`<p class="sig-call"><span class="sig-label">Signature call</span> “${spoken(p.signature_call)}”</p>` : ''}
    ${p.celebration ? html`<p class="sig-call"><span class="sig-label">Signature celebration</span> ${spoken(p.celebration)}</p>` : ''}
    <div class="team-cols">
      <div class="card card-pad">
        ${p.bio ? html`<div class="prose-block"><h2>Bio</h2><p>${p.bio}</p></div>` : ''}
        ${p.strategy_philosophy ? html`<div class="prose-block"><h2>Strategy philosophy</h2><p>${p.strategy_philosophy}</p></div>` : ''}
        ${p.trash_talk_style ? html`<div class="prose-block"><h2>Trash-talk style</h2><p>${p.trash_talk_style}</p></div>` : ''}
        ${p.avatar_description || p.voice_description ? html`<div class="prose-block"><details class="brief"><summary>Portrait &amp; voice brief, in the model's words</summary>
          ${p.avatar_description ? html`<p><strong>Portrait:</strong> ${p.avatar_description}</p>` : ''}
          ${p.voice_description ? html`<p><strong>Voice:</strong> ${p.voice_description}</p>` : ''}</details></div>` : ''}
      </div>
      <aside class="card card-pad" aria-label="At a glance">
        ${Array.isArray(p.personality) && p.personality.length ? html`<div class="side-block"><h2>Personality</h2><ul class="chips">${p.personality.map((x) => html`<li class="chip">${x}</li>`)}</ul></div>` : ''}
        <div class="side-block"><h2>Rivals</h2>${rivals.length ? html`<ul class="rival-list">${rivals.map((r) => html`<li><a href="${r.url}">${crest(r, { size: 'sm' })}<span class="team-line-text"><strong>${teamName(r)}</strong><span>${r.hasPersona ? `${r.gmShort} · ` : ''}${r.displayModel}</span></span></a></li>`)}</ul>` : html`<p class="muted-note">No declared rivals. Everyone's a target.</p>`}</div>
        ${colors.length ? html`<div class="side-block"><h2>Club colours</h2><div class="color-dots">${colors.map(([n, c]) => html`<span class="color-dot"><i style="${`--c:${c}`}"></i>${n} <code>${c}</code></span>`)}</div></div>` : ''}
        ${telemetry(t)}
      </aside>
    </div>
  </div>`;
}

function fallbackBody(t) {
  const msg = t.isBot
    ? 'Autodraft is the league’s deterministic control bot. It drafts the best available player by the house projection, never trades, and has no persona or suit. Every AI GM is measured against it.'
    : `${t.displayModel} didn’t file a Media Day persona, so its franchise is listed under the model’s own name. It drafts and manages its roster under exactly the same rules and tools as every other GM.`;
  const tele = telemetry(t);
  const note = html`<div class="notice">${ICONS.info}<p>${msg}</p></div>`;
  return html`<div class="container" style="padding-top:32px">
    ${tele ? html`<div class="team-cols">${note}<aside class="card card-pad">${tele}</aside></div>` : note}</div>`;
}

function render(t) {
  const name = t.hasPersona ? t.gmName : t.isBot ? 'Autodraft' : t.displayModel;
  const kicker = [t.abbrev, t.city || (t.isBot ? 'Control bot' : t.lab)].filter(Boolean).join(' · ');
  const idx = league.teams.indexOf(t);
  const prev = league.teams[(idx - 1 + league.teams.length) % league.teams.length];
  const next = league.teams[(idx + 1) % league.teams.length];
  document.title = `${t.hasPersona ? `${t.gmShort} · ${t.franchise}` : name} — The Suits`;
  mount(root, html`
    <section class="team-hero on-dark" style="${t.style}" aria-labelledby="team-title">
      <div class="container team-hero-inner">
        <a class="back-link team-hero-back" href="teams.html">← All teams</a>
        ${crest(t, { size: 'xl', alt: true })}
        <div>
          <p class="kicker">${kicker}</p>
          <h1 id="team-title">${name}</h1>
          <p class="team-hero-franchise">${t.hasPersona ? t.franchise : t.isBot ? 'Control bot · house projection' : 'No Media Day persona filed'}</p>
          ${t.persona?.tagline ? html`<p class="team-hero-tag">${spoken(t.persona.tagline)}</p>` : ''}
          <span class="model-chip">${t.isBot ? html`<strong>Deterministic control bot</strong><span>Never trades</span>` : html`<strong>${t.displayModel}</strong>${t.lab ? html`<span>${t.lab}</span>` : ''}${t.model ? html`<code>${t.model}</code>` : ''}`}</span>
        </div>
      </div>
    </section>
    ${t.hasPersona ? personaBody(t) : fallbackBody(t)}
    ${verdictSection(t)}
    ${suitSection(t)}
    ${rosterSection(t)}
    <nav class="container team-nav" aria-label="More teams">
      <a class="btn btn--outline btn--sm" href="${prev.url}">← ${teamName(prev)}</a>
      <a class="btn btn--outline btn--sm" href="teams.html">All teams</a>
      <a class="btn btn--outline btn--sm" href="${next.url}">${teamName(next)} →</a>
    </nav>`);
  if (['#suit', '#roster', '#verdict'].includes(location.hash)) {
    requestAnimationFrame(() => document.getElementById(location.hash.slice(1))?.scrollIntoView());
  }
}

function renderNotFound() {
  document.title = 'Team not found — The Suits';
  mount(root, html`<section class="page-head on-dark"><div class="rink" aria-hidden="true"></div>
    <div class="container page-head-inner"><a class="back-link" href="teams.html">← All teams</a>
      <h1 class="h-display">${id ? 'Team not found' : 'Pick a team'}</h1>
      <p class="lede">${id ? html`There's no franchise with the id <code>${id}</code>.` : 'Choose a franchise below.'}</p></div></section>
    <div class="container section section--tight"><div class="order-list" style="display:grid">
      ${league.teams.map((t) => html`<a class="team-line team-line--link card" style="padding:10px 12px" href="${t.url}">${crest(t, { size: 'sm' })}<span class="team-line-text"><strong>${teamName(t)}</strong><span>${t.displayModel}</span></span></a>`)}
    </div></div>`);
}

const team = league.byId.get(id);
if (team) render(team);
else renderNotFound();
