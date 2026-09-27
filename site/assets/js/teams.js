// Teams index: one card per franchise, plus the suit wall.
import './chrome.js';
import { $, html, spoken, mount, loadManifest, loadData, buildLeague, crest, swatchAttrs } from './lib.js';

const manifest = await loadManifest();
const [personas, draft, fabrics] = await Promise.all([loadData('personas.json'), loadData('draft.json'), loadData('fabrics.json')]);
const league = buildLeague({ personas, draft, fabrics, manifest });

function card(t) {
  let name = t.gmShort;
  let sub = html`${t.franchise}${t.city && !t.franchise.startsWith(t.city) ? html` · ${t.city}` : ''}`;
  let tag = spoken(t.persona?.tagline || '');
  if (t.isBot && !t.hasPersona) {
    name = 'Autodraft';
    sub = 'Control bot';
    tag = 'Takes the best available player by the house projection and never trades — the baseline every AI GM has to beat.';
  } else if (!t.hasPersona) {
    name = t.displayModel;
    sub = 'No Media Day persona filed';
    tag = 'This GM didn’t file a persona, so its franchise is listed under the model’s own name.';
  }
  return html`<a class="team-card" href="${t.url}" style="${t.style}">
    <div class="team-card-band">${crest(t, { size: 'lg' })}<span class="team-card-abbr" aria-hidden="true">${t.abbrev}</span></div>
    <div class="team-card-body">
      <h2>${name}</h2>
      <span class="team-card-franchise">${sub}</span>
      ${tag ? html`<p class="team-card-tag">${tag}</p>` : ''}
    </div>
    <div class="team-card-foot"><span><strong>${t.isBot ? 'Deterministic bot' : t.displayModel}</strong>${t.lab && !t.isBot ? ` · ${t.lab}` : ''}</span><span class="arrow" aria-hidden="true">→</span></div>
  </a>`;
}

const grid = $('#team-grid');
if (!league.teams.length) {
  mount(grid, html`<div class="empty"><h3>The GMs arrive at Media Day</h3><p>Personas, franchises and suits appear here once they're filed.</p></div>`);
} else {
  mount(grid, league.teams.map(card));
}

const suited = league.teams.filter((t) => t.fabric);
if (!suited.length) {
  $('#suit-wall').hidden = true;
} else {
  mount($('#suit-grid'), suited.map((t) => {
    const s = swatchAttrs(t.fabric, t.swatch);
    const f = t.fabric;
    return html`<a class="suit-tile" href="${t.url}#suit">
      <span class="${s.cls}" style="${s.style}" role="img" aria-label="${`Fabric swatch: ${f.color_name || ''} ${f.pattern_desc || ''}`.trim()}"></span>
      <div><strong>${t.gmShort}</strong><span>${[f.color_name, f.pattern_desc && f.pattern_desc !== 'solid' ? f.pattern_desc : ''].filter(Boolean).join(' · ') || f.name}</span><span>${t.franchise}</span></div>
    </a>`;
  }));
}
