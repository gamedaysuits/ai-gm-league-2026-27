// Report Card pieces shared by the draft board and team pages (post-draft peer grades from grades.json).
import { html, spoken, crest, gradeText, gradeSpeech, gradeTier, ordinal, spiciest, plural } from './lib.js';

export const teamName = (t) => (t.hasPersona ? t.franchise : t.isBot ? 'Autodraft' : t.displayModel);
const gmName = (t) => (t.hasPersona ? t.gmShort : t.isBot ? 'Autodraft' : t.displayModel);

export function gradeBadge(letter, gpa, { size = 'lg' } = {}) {
  const g = Number(gpa);
  return html`<div class="gbadge gbadge--${size} gr-${gradeTier(letter)}">
    <span class="gbadge-letter" aria-hidden="true">${gradeText(letter)}</span>
    <span class="gbadge-gpa" aria-hidden="true">GPA ${Number.isFinite(g) ? g.toFixed(2) : '–'}</span>
    <span class="sr-only">Class grade ${gradeSpeech(letter)}, GPA ${Number.isFinite(g) ? g.toFixed(2) : 'not available'}.</span>
  </div>`;
}

export const gradeChip = (g) => html`<span class="gchip gr-${gradeTier(g)}" aria-hidden="true">${gradeText(g)}</span><span class="sr-only">${gradeSpeech(g)}.</span>`;

/** One grade as read on air: grade, the line (delivery tags stripped) and who said it. */
export function quote(g, team) {
  const t = team(g.from);
  return html`<li class="rc-quote">${gradeChip(g.grade)}<div class="rc-quote-body">
    <p class="rc-line">“${spoken(g.comment)}”</p>
    <p class="rc-by">${crest(t, { size: 'xs' })}<span><strong>${gmName(t)}</strong> <span class="meta">${t.abbrev}</span></span></p>
  </div></li>`;
}

export function spreadLine(r) {
  if (!r.spread) return '';
  const range = r.high === r.low ? `Every grade: ${gradeText(r.high)}` : `${gradeText(r.high)} to ${gradeText(r.low)}`;
  return html`<p class="rc-spread"><span class="spread spread--${r.spread.key}">${r.spread.label}</span> <span class="meta">${range} · ${plural(r.grades.length, 'grade')}</span></p>`;
}

/** Everything a team received, spiciest first: the first `lead` shown, the rest behind a toggle. */
export function quoteList(r, team, { lead = 2 } = {}) {
  const hot = spiciest(r.grades);
  const rest = hot.slice(lead);
  return html`<ul class="rc-quotes">${hot.slice(0, lead).map((g) => quote(g, team))}</ul>
    ${rest.length ? html`<details class="rc-all"><summary><span class="when-closed">Show all ${r.grades.length} grades</span><span class="when-open">Show fewer</span></summary>
      <ul class="rc-quotes">${rest.map((g) => quote(g, team))}</ul></details>` : ''}`;
}

export function reportCard(report, team) {
  return report.rows.map((r) => {
    const t = team(r.team);
    const sub = t.hasPersona ? `${t.gmShort} · ${t.displayModel}` : t.isBot ? 'Control bot' : t.lab || t.displayModel;
    return html`<li class="rc-card" style="${t.style}">
      <div class="rc-head">
        <span class="rc-rank"><span class="sr-only">Rank </span>${r.rank}</span>
        <a class="team-line team-line--link" href="${t.url}#verdict">${crest(t, { size: 'sm' })}<span class="team-line-text"><strong>${teamName(t)}</strong><span>${sub}</span></span></a>
        ${gradeBadge(r.letter, r.gpa)}
      </div>
      ${spreadLine(r)}
      ${quoteList(r, team)}
    </li>`;
  });
}

export function delusionIndex(report, team) {
  if (!report.filed) return '';
  const line = (label, entry, verb) => {
    const t = team(entry.row.team);
    return html`<p class="delusion-line"><strong>${label}</strong> ${gmName(t)} has ${t.hasPersona ? `the ${t.franchise}` : 'itself'} finishing ${ordinal(entry.row.self)}; ${verb} ${ordinal(entry.row.league)}.</p>`;
  };
  return html`<aside class="delusion on-dark" aria-labelledby="delusion-title">
    <p class="kicker" id="delusion-title">Delusion index</p>
    <div class="delusion-stats">
      <p><strong>${report.first}</strong><span>of ${report.filed} GMs picked themselves to win the league</span></p>
      <p><strong>${report.top3}</strong><span>of ${report.filed} put themselves in the top 3</span></p>
    </div>
    ${report.deluded ? line('Most delusional:', report.deluded, 'the rest of the league says') : ''}
    ${report.humble ? line('Most humble:', report.humble, 'the rest of the league has them') : ''}
  </aside>`;
}
