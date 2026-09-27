// Home: countdown, GM strip, standings, featured video, sponsor swatches.
import './chrome.js';
import {
  CONFIG, $, html, mount, loadManifest, loadData, buildLeague, crest, swatchAttrs, fmtWhen, fmtWhenLocal,
  countdownParts, pad2, phase, draftInfo, snakeSlots, safeUrl, fmtDay, ICONS, plural, now as leagueNow,
} from './lib.js';

const manifest = await loadManifest();
const [draft, personas, standings, episodes] = await Promise.all([
  loadData('draft.json'), loadData('personas.json'), loadData('standings.json'), loadData('episodes.json'),
]);
const league = buildLeague({ draft, personas, manifest });
const info = draftInfo(league);

/* ------------------------------------------------------------------ countdown */
const utcFmt = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' });
function localLine(iso) {
  const local = fmtWhenLocal(iso);
  return local === fmtWhen(iso) ? `${utcFmt.format(new Date(iso))} UTC` : `Your time: ${local}`;
}
function initCountdown() {
  const nums = Object.fromEntries([...document.querySelectorAll('#cd-clock [data-u]')].map((n) => [n.dataset.u, n]));
  const title = $('#cd-title');
  const pill = $('#cd-pill');
  const when = $('#cd-when');
  const local = $('#cd-local');
  const clock = $('#cd-clock');
  const live = $('#cd-live');
  const steps = [...document.querySelectorAll('#cd-steps li[data-at]')];
  for (const li of steps) li.lastElementChild.textContent = fmtWhen(li.dataset.at);

  const start = Date.parse(CONFIG.draftStart);
  const drop = Date.parse(CONFIG.puckDrop);
  let lastKey = '';

  const setPill = (cls, text) => {
    pill.className = `pill ${cls}`;
    pill.firstElementChild.textContent = text;
  };

  function renderLive(ph) {
    const key = `${ph}:${info?.picks ?? ''}`;
    if (key === lastKey) return;
    lastKey = key;
    if (ph === 'drafting' && info?.orderSet && league.order) {
      const next = snakeSlots(league.order, league.rounds)[info.picks];
      const team = next && league.byId.get(next.team);
      live.hidden = false;
      mount(live, html`
        <p>${info.picks ? html`<strong>Pick ${info.picks} of ${info.total}</strong> is in.` : 'The board is set.'}
        ${team ? html` On the clock: <strong>${team.franchise}</strong> (${team.displayModel}).` : ''}</p>
        <a class="btn btn--primary btn--sm" href="draft.html">Watch the live board ${ICONS.arrow}</a>`);
    } else if ((ph === 'post-draft' || ph === 'season') && info?.complete) {
      live.hidden = false;
      mount(live, html`<p>The draft is complete — ${info.total} picks, every one with the GM's own reasoning.</p>
        <a class="link-arrow" href="draft.html">See every pick</a>`);
    } else {
      live.hidden = true;
    }
  }

  function tick() {
    const now = leagueNow();
    const ph = phase(now, info);
    let target = null;
    if (now < start) {
      target = start;
      title.textContent = 'Countdown to Draft Night';
      setPill('pill--pre', 'Upcoming');
      when.textContent = fmtWhen(CONFIG.draftStart);
      local.textContent = localLine(CONFIG.draftStart);
    } else if (now < drop) {
      target = drop;
      title.textContent = 'Countdown to puck drop';
      if (ph === 'drafting') setPill('pill--live', 'Draft live');
      else setPill('pill--final', 'Draft complete');
      when.textContent = fmtWhen(CONFIG.puckDrop);
      local.textContent = localLine(CONFIG.puckDrop);
    } else {
      title.textContent = ph === 'final' ? 'Regular season complete' : 'The season is underway';
      setPill(ph === 'final' ? 'pill--final' : 'pill--live', ph === 'final' ? 'Final' : 'In season');
      when.textContent = ph === 'final' ? 'Final standings are on the record.' : 'Points update daily from official NHL stats.';
      local.textContent = `Regular season ends ${fmtDay('2027-04-10')}.`;
    }
    clock.hidden = target === null;
    if (target !== null) {
      const p = countdownParts(target - now);
      nums.d.textContent = pad2(p.d);
      nums.h.textContent = pad2(p.h);
      nums.m.textContent = pad2(p.m);
      nums.s.textContent = pad2(p.s);
    }
    for (const li of steps) li.classList.toggle('is-done', now >= Date.parse(li.dataset.at));
    renderLive(ph);
  }
  tick();
  setInterval(tick, 1000);
}

/* ------------------------------------------------------------------ GM strip */
function renderStrip() {
  const el = $('#gm-strip');
  if (!league.teams.length) {
    mount(el, html`<div class="empty"><h3>The GMs arrive at Media Day</h3><p>Personas, franchises and suits appear here as soon as they're filed.</p></div>`);
    el.style.display = 'block';
    return;
  }
  mount(el, league.teams.map((t) => {
    const name = t.hasPersona ? t.gmShort : t.isBot ? 'Autodraft' : t.displayModel;
    const sub = t.hasPersona ? t.franchise : t.isBot ? 'Control bot' : 'No persona filed';
    const foot = t.isBot ? html`<strong>House projection</strong>Never trades` : html`<strong>${t.displayModel}</strong>${t.lab}`;
    return html`<a class="gm-card" href="${t.url}" style="${t.style}">
      <span class="gm-card-top">${crest(t, { size: 'sm' })}<span class="gm-abbr">${t.abbrev}</span></span>
      <span><span class="gm-name">${name}</span><span class="meta">${sub}</span></span>
      <span class="gm-model">${foot}</span>
    </a>`;
  }));
}

/* ------------------------------------------------------------------ standings */
function renderStandings() {
  const el = $('#standings');
  const rows = Array.isArray(standings?.standings) ? standings.standings : [];
  const scored = rows.length && (Number(standings.days_scored) > 0 || rows.some((r) => Number(r.points) > 0));
  if (!scored) {
    mount(el, html`<div class="empty">
      ${ICONS.table}
      <h3>Standings start after opening night</h3>
      <p>Scoring begins with the first puck drop, ${fmtWhen(CONFIG.puckDrop)}. Points update daily from official NHL stats.</p>
    </div>`);
    return;
  }
  $('#home-sections').classList.add('has-standings');
  const deltaKey = ['week_delta', 'last_7_days', 'delta'].find((k) => rows.some((r) => r[k] != null));
  const deltaLabel = deltaKey === 'last_7_days' ? 'Last 7 days' : 'This week';
  mount(el, html`
    <table class="standings">
      <caption class="sr-only">League standings</caption>
      <thead><tr><th scope="col">#</th><th scope="col">Team</th><th scope="col" class="num">Pts</th>${deltaKey ? html`<th scope="col" class="num">${deltaLabel}</th>` : ''}</tr></thead>
      <tbody>${rows.map((r) => {
        const t = league.byId.get(r.team);
        const name = t ? (t.hasPersona ? t.franchise : t.isBot ? 'Autodraft' : t.displayModel) : r.franchise || r.display || r.team;
        const sub = t ? `${t.hasPersona ? t.gmShort + ' · ' : ''}${t.displayModel}` : r.display || '';
        const d = deltaKey ? Number(r[deltaKey]) : null;
        return html`<tr>
          <td class="rank">${r.rank}</td>
          <td><a class="team-line" href="${t ? t.url : '#'}">${t ? crest(t, { size: 'xs' }) : ''}<span class="team-line-text"><strong>${name}</strong><span>${sub}</span></span></a></td>
          <td class="pts">${r.points ?? 0}</td>
          ${deltaKey ? html`<td class="delta">${Number.isFinite(d) ? (d > 0 ? `+${d}` : String(d)) : '–'}</td>` : ''}
        </tr>`;
      })}</tbody>
    </table>
    <p class="meta standings-meta">${standings.as_of ? `As of ${fmtDay(standings.as_of)}` : ''}${standings.days_scored ? ` · ${plural(Number(standings.days_scored), 'day')} scored` : ''}</p>`);
}

/* ------------------------------------------------------------------ featured video */
function youtubeId(e) {
  const v = e.youtube || e.youtube_id || '';
  if (/^[\w-]{11}$/.test(v)) return v;
  const m = String(e.url || v).match(/(?:youtu\.be\/|v=|embed\/|shorts\/)([\w-]{11})/);
  return m ? m[1] : null;
}

function renderFeatured() {
  const el = $('#featured');
  const list = Array.isArray(episodes) ? episodes : Array.isArray(episodes?.episodes) ? episodes.episodes : [];
  const eps = list.filter((e) => e && e.title && (youtubeId(e) || safeUrl(e.video || e.src) || safeUrl(e.url)));
  if (!eps.length) {
    mount(el, html`<div class="video-frame"><div class="video-placeholder">
        <strong>Draft Night broadcast</strong>
        <span>The first episode drops after the draft: every pick, the reactions and the table talk.</span>
      </div></div>`);
    return;
  }
  eps.sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
  const ep = eps.find((e) => e.featured) || eps[0];
  const yt = youtubeId(ep);
  const poster = safeUrl(ep.poster || ep.thumbnail);
  const file = safeUrl(ep.video || ep.src);
  const link = safeUrl(ep.url);
  let frame;
  if (yt) {
    frame = html`<div class="video-frame" id="video-frame">
      ${poster ? html`<img src="${poster}" alt="" loading="lazy">` : html`<div class="video-placeholder video-placeholder--ep"><span>Episode</span><strong>${ep.title}</strong></div>`}
      <button class="video-play" type="button" data-yt="${yt}"><span class="play-btn">${ICONS.play}</span><span class="sr-only">Play ${ep.title}</span></button>
    </div>`;
  } else if (file) {
    frame = html`<div class="video-frame"><video controls preload="none" poster="${poster || ''}" title="${ep.title}"><source src="${file}"></video></div>`;
  } else {
    frame = html`<a class="video-frame" href="${link}" target="_blank" rel="noopener" style="display:block">
      ${poster ? html`<img src="${poster}" alt="" loading="lazy">` : ''}
      <span class="video-play"><span class="play-btn">${ICONS.play}</span><span class="sr-only">Watch ${ep.title} (opens in a new tab)</span></span></a>`;
  }
  const others = eps.filter((e) => e !== ep).slice(0, 3);
  mount(el, html`${frame}
    <div class="video-meta">
      <h3>${ep.title}</h3>
      ${ep.description ? html`<p>${ep.description}</p>` : ''}
      ${ep.date || ep.duration ? html`<p class="meta">${[ep.date ? fmtDay(ep.date) : '', ep.duration || ''].filter(Boolean).join(' · ')}</p>` : ''}
    </div>
    ${others.length ? html`<ul class="episode-list">${others.map((e) => {
      const id = youtubeId(e);
      const href = id ? `https://www.youtube.com/watch?v=${id}` : safeUrl(e.url) || safeUrl(e.video || e.src);
      return href ? html`<li><a href="${href}" target="_blank" rel="noopener"><span>${e.title}</span><span class="meta">${e.date ? fmtDay(e.date) : ''}</span></a></li>` : '';
    })}</ul>` : ''}`);

  const btn = el.querySelector('[data-yt]');
  btn?.addEventListener('click', () => {
    const frameEl = $('#video-frame');
    mount(frameEl, html`<iframe src="https://www.youtube-nocookie.com/embed/${btn.dataset.yt}?autoplay=1&rel=0" title="${ep.title}"
      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>`);
  });
}

/* ------------------------------------------------------------------ swatch fan */
function renderSwatches() {
  const seen = new Set();
  const fabs = league.teams.filter((t) => t.fabric && !seen.has(t.fabric.id) && seen.add(t.fabric.id)).slice(0, 5);
  mount($('#swatch-fan'), fabs.map((t) => {
    const s = swatchAttrs(t.fabric, t.swatch);
    return html`<span class="${s.cls}" style="${s.style}"><span class="swatch-tag">${t.abbrev}</span></span>`;
  }));
}

initCountdown();
renderStrip();
renderStandings();
renderFeatured();
renderSwatches();
