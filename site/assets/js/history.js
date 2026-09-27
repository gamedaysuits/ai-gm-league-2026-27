// History: past GDS AI pools from data/playoffs-2026.json and data/regular-2025-26.json
// (falls back to the synced Markdown write-ups, then to a placeholder).
import './chrome.js';
import { $, html, raw, mount, loadManifest, loadData, fetchText, fmtDay, ICONS } from './lib.js';

const manifest = await loadManifest();
const [playoffs, regular] = await Promise.all([loadData('playoffs-2026.json'), loadData('regular-2025-26.json')]);

// Friendly names for champion headlines; tables always show the exact model slug.
const MODEL_NAMES = {
  'google/gemini-3.1-pro-preview': 'Gemini 3.1 Pro',
  'meta-llama/llama-4-maverick': 'Llama 4 Maverick',
  'openai/gpt-5.4': 'GPT-5.4',
  'x-ai/grok-4': 'Grok 4',
  'anthropic/claude-3.5-sonnet': 'Claude 3.5 Sonnet',
};
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const money = (v) => (num(v) == null ? '–' : `$${num(v).toLocaleString('en-US')}`);
const dash = (v) => (v == null || v === '' ? '–' : v);
const byRank = (teams, key) => [...teams].sort((a, b) => (num(a[key]) ?? 99) - (num(b[key]) ?? 99));

/* ------------------------------------------------------------------ champions */
function champPlayoffs(d) {
  const teams = byRank(d.teams, 'rank');
  const [w, r2] = teams;
  if (!w) return '';
  const name = MODEL_NAMES[w.model] || w.team;
  const r2name = r2 ? MODEL_NAMES[r2.model] || r2.team : '';
  const margin = r2 ? num(w.points) - num(r2.points) : null;
  const s = d.stats || {};
  return html`<article class="champ">
    <p class="kicker">${d.pool || 'AI Playoff Pool'}</p>
    <div class="champ-trophy">${ICONS.trophy}<div><h2>${name}</h2>${w.nickname ? html`<span class="champ-nick">“${w.nickname}”</span>` : ''}</div></div>
    <div class="champ-score"><strong>${w.points}</strong><span>points · champion</span></div>
    <p>${teams.length} AI GMs drafted ${w.players?.length || 'their'} players each for the playoffs. ${name} finished on top${r2 && margin != null ? `, ${margin === 0 ? 'level with' : `${margin} point${margin === 1 ? '' : 's'} clear of`} ${r2name} (${r2.points})` : ''}.</p>
    ${s.games ? html`<p class="champ-meta">${s.games} playoff games · ${fmtDay(s.first_game)} – ${fmtDay(s.last_game)} · ${s.total_goals} goals scored</p>` : ''}
  </article>`;
}

function champRegular(d) {
  const ranked = byRank(d.teams, 'rank_best10_minus_unspent');
  const [w, r2] = ranked;
  if (!w) return '';
  const rawLeader = byRank(d.teams, 'rank_raw')[0];
  const rules = d.rules || {};
  const scoring = rules.variants?.best10_minus_unspent || 'best 10, minus 1 point per $10 of budget left unspent';
  const upset = rawLeader && rawLeader.team !== w.team;
  return html`<article class="champ">
    <p class="kicker">2025-26 regular-season auction</p>
    <div class="champ-trophy">${ICONS.trophy}<div><h2>${w.model || w.team}</h2><span class="champ-nick">Won under the rules given to the models</span></div></div>
    <div class="champ-score"><strong>${w.best10_minus_unspent}</strong>${r2 ? html`<span>to ${r2.best10_minus_unspent} over ${r2.model || r2.team}</span>` : ''}</div>
    <p>Scored as ${scoring}.${upset ? ` ${rawLeader.model} actually outscored ${w.model} on raw points, ${rawLeader.raw}–${w.raw}, but left ${money(rawLeader.unspent)} of its ${money(rules.budget)} budget on the table.` : ''}</p>
    <p class="champ-meta">${d.teams.length} AI GMs · ${money(rules.budget)} budget · ${rules.roster_size || 11} skaters each${d.stats?.games ? ` · ${d.stats.games} NHL games` : ''}</p>
  </article>`;
}

/* ------------------------------------------------------------------ full results */
function playoffsSection(d) {
  const teams = byRank(d.teams, 'rank');
  const top = teams[0]?.team;
  const s = d.stats || {};
  return html`<section class="pool" id="playoffs-2026" aria-labelledby="po-title">
    <div class="pool-head">
      <div><p class="kicker">Spring 2026 · ${teams.length} AI GMs</p><h2 class="h-section" id="po-title">${d.pool || '2026 AI Playoff Pool'}</h2></div>
      <p>${d.rules?.notes ? `Scoring: ${d.rules.notes}.` : ''} ${d.rules?.tiebreak ? `Tiebreak: ${d.rules.tiebreak}${s.total_goals ? ` (${s.total_goals})` : ''}.` : ''}</p>
    </div>
    <div class="table-scroll"><table class="hist-table">
      <caption class="sr-only">${d.pool} final standings</caption>
      <thead><tr><th scope="col" class="num">Rank</th><th scope="col">GM</th><th scope="col">Model</th><th scope="col" class="num">Pts</th><th scope="col" class="num">Goals guess</th><th scope="col" class="num">Off by</th></tr></thead>
      <tbody>${teams.map((t) => html`<tr class="${t.team === top ? 'is-champ' : ''}">
        <td class="num">${t.rank}</td>
        <td class="name">${t.team}<small>${t.nickname || ''}</small></td>
        <td><code>${t.model}</code></td>
        <td class="num big">${t.points}</td>
        <td class="num">${dash(t.goals_guess)}</td>
        <td class="num">${dash(t.guess_off_by)}</td></tr>`)}</tbody>
    </table></div>
    ${Array.isArray(d.tiebreaks) && d.tiebreaks.length ? html`<ul class="note-list">${d.tiebreaks.map((x) => html`<li>${x}</li>`)}</ul>` : ''}
    <div class="rosters">${teams.map((t) => html`<details>
      <summary><span>${t.rank}. ${t.team}${t.nickname ? ` — ${t.nickname}` : ''}</span><span class="meta">${t.points} pts</span></summary>
      <div class="table-scroll"><table>
        <thead><tr><th scope="col" class="num">Pick</th><th scope="col">Player</th><th scope="col">Club</th><th scope="col">Pos</th><th scope="col" class="num">GP</th><th scope="col" class="num">G</th><th scope="col" class="num">A</th><th scope="col" class="num">W</th><th scope="col" class="num">SO</th><th scope="col" class="num">Pts</th></tr></thead>
        <tbody>${(t.players || []).map((p) => {
          const g = p.position === 'G';
          return html`<tr><td class="num">${p.pick}</td><td>${p.player}${p.no_playoff_games ? html` <span class="meta">(no playoff games)</span>` : ''}</td><td>${p.club}</td><td>${p.position}</td>
            <td class="num">${p.gp}</td><td class="num">${g ? '–' : p.goals}</td><td class="num">${g ? '–' : p.assists}</td>
            <td class="num">${g ? p.wins : '–'}</td><td class="num">${g ? p.shutouts : '–'}</td><td class="num"><strong>${p.points}</strong></td></tr>`;
        })}</tbody></table></div>
    </details>`)}</div>
    ${s.source ? html`<p class="source-note">Source: ${s.source}. ${s.games ? `${s.games} games, ${s.player_game_lines} player-game lines` : ''}${s.mismatches_vs_nhl_playoff_totals != null ? `, ${s.mismatches_vs_nhl_playoff_totals} mismatches against NHL playoff totals` : ''}.</p>` : ''}
  </section>`;
}

function regularSection(d) {
  const teams = byRank(d.teams, 'rank_best10_minus_unspent');
  const rules = d.rules || {};
  const s = d.stats || {};
  const top = teams[0]?.team;
  return html`<section class="pool" id="regular-2025-26" aria-labelledby="rs-title">
    <div class="pool-head">
      <div><p class="kicker">October 2025 auction · ${teams.length} AI GMs</p><h2 class="h-section" id="rs-title">${d.pool || 'GDS AI Hockey Draft 2025-26'}</h2></div>
      <p>${money(rules.budget)} budget, ${rules.roster_size || 11} skaters each, scored on ${rules.points || 'NHL regular-season goals + assists'}. Ranked by ${rules.variants?.best10_minus_unspent || 'best 10 minus unspent'}; the other columns show each variant's rank in brackets.</p>
    </div>
    <div class="table-scroll"><table class="hist-table">
      <caption class="sr-only">${d.pool} final standings</caption>
      <thead><tr><th scope="col" class="num">Rank</th><th scope="col">GM</th><th scope="col">Model</th><th scope="col" class="num">Spent</th><th scope="col" class="num">Best 10 − unspent</th><th scope="col" class="num">Best 10</th><th scope="col" class="num">Raw</th></tr></thead>
      <tbody>${teams.map((t) => html`<tr class="${t.team === top ? 'is-champ' : ''}">
        <td class="num">${t.rank_best10_minus_unspent}</td>
        <td class="name">${t.team}</td>
        <td>${t.model}<br><code>${t.model_slug}</code></td>
        <td class="num">${money(t.spent)}</td>
        <td class="num big">${t.best10_minus_unspent}</td>
        <td class="num">${t.best10}<span class="rk">(${t.rank_best10})</span></td>
        <td class="num">${t.raw}<span class="rk">(${t.rank_raw})</span></td></tr>`)}</tbody>
    </table></div>
    <div class="rosters">${teams.map((t) => html`<details>
      <summary><span>${t.rank_best10_minus_unspent}. ${t.model || t.team}</span><span class="meta">${t.best10_minus_unspent} · spent ${money(t.spent)}${num(t.unspent) ? ` · ${money(t.unspent)} unspent (−${Math.floor(num(t.unspent) / 10)})` : ''}</span></summary>
      <div class="table-scroll"><table>
        <thead><tr><th scope="col">Player</th><th scope="col">Club</th><th scope="col">Pos</th><th scope="col" class="num">Price</th><th scope="col" class="num">GP</th><th scope="col" class="num">G</th><th scope="col" class="num">A</th><th scope="col" class="num">Pts</th></tr></thead>
        <tbody>${(t.players || []).map((p) => html`<tr><td>${p.player}${p.in_best10 === false ? html` <span class="meta">(dropped from best 10)</span>` : ''}</td><td>${p.nhl_clubs || p.club_at_draft}</td><td>${p.position}</td>
          <td class="num">${money(p.price)}</td><td class="num">${p.gp}</td><td class="num">${p.goals}</td><td class="num">${p.assists}</td><td class="num"><strong>${p.points}</strong></td></tr>`)}</tbody>
      </table></div>
    </details>`)}</div>
    ${s.source ? html`<p class="source-note">Source: ${s.source}. ${s.games ? `${s.games} games, ${s.player_game_lines} player-game lines` : ''}${s.mismatches_vs_summed_game_lines != null ? `, ${s.mismatches_vs_summed_game_lines} mismatches against summed game lines` : ''}.</p>` : ''}
  </section>`;
}

async function fallback(stem, title) {
  const path = manifest?.fragments?.[stem];
  if (path) {
    try {
      const frag = await fetchText(path);
      return html`<section class="pool" id="${stem}"><div class="prose src-md">${raw(frag)}</div></section>`;
    } catch { /* fall through to placeholder */ }
  }
  return html`<section class="pool" id="${stem}"><div class="empty"><h3>${title}</h3><p>Full results are being rescored from official NHL stats and will appear here soon.</p></div></section>`;
}

const valid = (d) => d && Array.isArray(d.teams) && d.teams.length;
if (valid(playoffs) || valid(regular)) {
  const cards = [valid(playoffs) ? champPlayoffs(playoffs) : null, valid(regular) ? champRegular(regular) : null];
  const current = [...document.querySelectorAll('#champs .champ')];
  cards.forEach((c, i) => {
    if (!c || !current[i]) return;
    const tmp = document.createElement('div');
    mount(tmp, c);
    current[i].replaceWith(tmp.firstElementChild);
  });
}
const sections = await Promise.all([
  valid(playoffs) ? playoffsSection(playoffs) : fallback('playoffs-2026', '2026 AI Playoff Pool'),
  valid(regular) ? regularSection(regular) : fallback('regular-2025-26', 'GDS AI Hockey Draft 2025-26'),
]);
mount($('#pools'), sections);
