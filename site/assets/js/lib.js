// Shared helpers for The Suits site: config, data loading, league model, safe HTML templating.

export const CONFIG = {
  orderReveal: '2026-09-27T18:00:00Z', // drand round 32576212
  draftStart: '2026-09-28T14:00:00Z',  // Mon Sep 28, 08:00 MDT
  puckDrop: '2026-09-29T21:00:00Z',    // first puck drop of the 2026-27 NHL season
  seasonEnd: '2027-04-10T23:59:00-04:00',
  tz: 'America/Edmonton',
  pollMs: 15000,
  rounds: 14,
  sponsorUrl: 'https://gamedaysuits.ca',
  drandRound: 32576212,
};

/** League clock. Add ?now=2026-09-28T16:00:00Z to any page to preview how it looks at that moment. */
const NOW_OFFSET = (() => {
  try {
    const q = new URLSearchParams(globalThis.location?.search || '').get('now');
    const t = q ? Date.parse(q) : NaN;
    return Number.isFinite(t) ? t - Date.now() : 0;
  } catch {
    return 0;
  }
})();
export const now = () => Date.now() + NOW_OFFSET;

/* ------------------------------------------------------------------ safe HTML */
class SafeHTML {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
export const raw = (s) => new SafeHTML(String(s ?? ''));
/** On-air text without the GM's voice-delivery tags like [shouting]. */
export const spoken = (s) => String(s ?? '').replace(/\[[^\[\]]{1,30}\]/g, ' ').replace(/\s+/g, ' ').trim();
function toHTML(v) {
  if (v == null || v === false || v === true) return '';
  if (v instanceof SafeHTML) return v.s;
  if (Array.isArray(v)) return v.map(toHTML).join('');
  return esc(v);
}
/** Tagged template: every interpolation is escaped unless wrapped in raw() or produced by html``. */
export function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += toHTML(vals[i]) + strings[i + 1];
  return new SafeHTML(out);
}
export function mount(el, content) {
  if (el) el.innerHTML = toHTML(content);
  return el;
}
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Only same-site relative paths or https URLs make it into src/href attributes. */
export function safeUrl(u, { allowExternal = true } = {}) {
  if (typeof u !== 'string' || !u.trim()) return null;
  const s = u.trim();
  if (/^https:\/\//i.test(s)) return allowExternal ? s : null;
  if (/^(?:\.{0,2}\/)?[\w\-./%]+(?:\?[\w\-=&.%]*)?(?:#[\w-]*)?$/.test(s) && !s.includes('..') && !s.startsWith('//')) return s;
  return null;
}

/* ------------------------------------------------------------------ data */
const DATA = 'data/';
let manifestPromise = null;

export async function fetchJSON(url, { bust = false } = {}) {
  const u = bust ? `${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}` : url;
  const res = await fetch(u, { cache: bust ? 'no-store' : 'no-cache' });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.json();
}

export async function fetchText(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.text();
}

/** data/manifest.json lists what the last sync published; null if the site was never synced. */
export function loadManifest() {
  manifestPromise ??= fetchJSON(DATA + 'manifest.json', { bust: true }).catch(() => null);
  return manifestPromise;
}

/** Fetch data/<name> if the manifest says it exists. Resolves to null on absence or error. */
export async function loadData(name, { bust = false } = {}) {
  const m = await loadManifest();
  if (m && !(m.files && m.files[name])) return null;
  try {
    return await fetchJSON(DATA + name, { bust });
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ colour */
const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;
export const validHex = (c) => (typeof c === 'string' && HEX.test(c.trim()) ? c.trim() : null);
function rgb(hex) {
  let h = hex.slice(1);
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}
export function luminance(hex) {
  const [r, g, b] = rgb(hex).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
}
/** Readable text colour for a background: white if it passes AA, else brand navy (or whichever is higher). */
export function inkOn(bg) {
  const white = contrast(bg, '#FFFFFF');
  if (white >= 4.5) return '#FFFFFF';
  const navy = contrast(bg, '#0E1B4D');
  return navy >= white ? '#0E1B4D' : '#FFFFFF';
}

/* ------------------------------------------------------------------ people & teams */
const QUOTED = /\s*["'‘“]([^"'’”]{2,40})["'’”]\s*/;
export function shortName(name) {
  return String(name || '').replace(QUOTED, ' ').replace(/\s+/g, ' ').trim();
}
export function nickname(name) {
  const m = String(name || '').match(QUOTED);
  return m ? m[1].trim() : null;
}
export function initials(name) {
  const all = shortName(name).split(/[\s-]+/).filter((w) => /[A-Za-z0-9]/.test(w));
  const alpha = all.filter((w) => /^[A-Za-z]/.test(w));
  const words = alpha.length ? alpha : all;
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export const POS = { C: 'Centre', L: 'Left wing', R: 'Right wing', D: 'Defence', G: 'Goalie', F: 'Forward' };
export const POS_SHORT = { C: 'C', L: 'LW', R: 'RW', D: 'D', G: 'G', F: 'F' };
export const GROUP = { F: 'Forwards', D: 'Defence', G: 'Goalies' };
export const groupOf = (pos, group) => (group && GROUP[group] ? group : pos === 'D' ? 'D' : pos === 'G' ? 'G' : 'F');

function decorate(t, { fabricById, manifest }) {
  const p = t.persona && typeof t.persona === 'object' ? t.persona : null;
  t.persona = p;
  t.isBot = t.id === 'autodraft' || t.lab === 'Control' || (!t.model && /control/i.test(t.display || ''));
  t.hasPersona = !!p;
  t.displayModel = t.display || t.model || t.id;
  t.lab = t.lab || '';
  t.gmName = p?.gm_name || (t.isBot ? 'Autodraft' : t.displayModel);
  t.gmShort = shortName(t.gmName);
  t.gmNick = nickname(p?.gm_name);
  t.gmLabel = p ? t.gmShort : t.isBot ? 'Control bot' : 'No persona filed';
  t.franchise = p?.franchise_name || (t.isBot ? 'Autodraft' : t.displayModel);
  t.city = p?.franchise_city || '';
  t.abbrev = String(p?.franchise_abbrev || (t.isBot ? 'BOT' : t.id.slice(0, 3))).toUpperCase().slice(0, 4);
  t.primary = validHex(p?.primary_color) || (t.isBot ? '#4B5475' : '#2A3566');
  t.secondary = validHex(p?.secondary_color) || (t.isBot ? '#AEB6D3' : '#8FA8EA');
  t.ink = inkOn(t.primary);
  t.initials = t.isBot && !p ? 'AD' : initials(p ? t.gmShort : t.displayModel);
  const fid = p?.suit?.fabric_id;
  t.fabric = t.fabric || (fid ? fabricById.get(fid) : null) || null;
  const av = manifest?.avatars?.[t.id];
  t.avatar = safeUrl(av, { allowExternal: false });
  t.swatch = t.fabric ? safeUrl(manifest?.swatches?.[t.fabric.id], { allowExternal: false }) : null;
  t.url = `team.html?id=${encodeURIComponent(t.id)}`;
  t.style = `--tp:${t.primary};--ts:${t.secondary};--ti:${t.ink}`;
  t.roster = Array.isArray(t.roster) ? t.roster : [];
  return t;
}

/**
 * Merge personas.json (persona + joined fabric) and draft.json (order, roster, picks, says)
 * into one league model. Either source may be missing.
 */
export function buildLeague({ draft = null, personas = null, fabrics = null, manifest = null } = {}) {
  const byId = new Map();
  const get = (id) => {
    if (!byId.has(id)) byId.set(id, { id });
    return byId.get(id);
  };
  for (const t of personas?.teams ?? []) {
    const id = t.team ?? t.id;
    if (!id) continue;
    const x = get(id);
    Object.assign(x, { display: t.display, lab: t.lab, model: t.model });
    if (t.persona) x.persona = t.persona;
    if (t.fabric) x.fabric = t.fabric;
  }
  for (const t of draft?.teams ?? []) {
    if (!t?.id) continue;
    const x = get(t.id);
    x.display ??= t.display;
    x.lab ??= t.lab;
    x.model ??= t.model;
    if (!x.persona && t.persona) x.persona = t.persona;
    x.roster = t.roster ?? [];
  }
  const fabricList = Array.isArray(fabrics) ? fabrics : fabrics?.fabrics ?? [];
  const fabricById = new Map(fabricList.filter((f) => f && f.id).map((f) => [f.id, f]));
  const rawOrder = Array.isArray(draft?.order) ? draft.order.filter((id) => byId.has(id)) : [];
  const order = rawOrder.length ? rawOrder : null;
  const ids = order ? [...order, ...[...byId.keys()].filter((id) => !order.includes(id))] : [...byId.keys()];
  const teams = ids.map((id) => decorate(byId.get(id), { fabricById, manifest }));
  const rounds = Number(draft?.rounds) || CONFIG.rounds;
  const picks = (Array.isArray(draft?.picks) ? draft.picks : []).filter((p) => p && Number.isFinite(p.pick_no));
  const says = Array.isArray(draft?.says) ? draft.says.filter((s) => s && s.line) : [];
  return {
    teams,
    byId: new Map(teams.map((t) => [t.id, t])),
    order,
    rounds,
    picks,
    says,
    draft,
    manifest,
    total: order ? order.length * rounds : null,
  };
}

/** Snake order: odd rounds run left-to-right through `order`, even rounds right-to-left. */
export function snakeSlots(order, rounds) {
  const n = order.length;
  const out = [];
  for (let r = 1; r <= rounds; r++) {
    for (let i = 0; i < n; i++) {
      const col = r % 2 ? i : n - 1 - i;
      out.push({ pick_no: (r - 1) * n + i + 1, round: r, inRound: i + 1, team: order[col], col });
    }
  }
  return out;
}

/* ------------------------------------------------------------------ rendering bits */
export function crest(t, { size = 'md', alt = false } = {}) {
  const cls = `crest crest--${size}${t.avatar ? ' crest--img' : ''}`;
  const label = `${t.gmName}${t.hasPersona ? `, GM of the ${t.franchise}` : ''} (${t.displayModel})`;
  if (t.avatar) {
    const altText = alt ? `AI-generated portrait of ${label}` : '';
    return html`<span class="${cls}" style="${t.style}"><img src="${t.avatar}" alt="${altText}" loading="lazy" decoding="async" width="160" height="160"></span>`;
  }
  return alt
    ? html`<span class="${cls}" style="${t.style}" role="img" aria-label="${label}">${t.initials}</span>`
    : html`<span class="${cls}" style="${t.style}" aria-hidden="true">${t.initials}</span>`;
}

const STRIPE_WORDS = [
  [/burgundy|wine|red/i, 'rgba(176, 58, 72, .75)'],
  [/pink/i, 'rgba(222, 150, 170, .6)'],
  [/purple/i, 'rgba(150, 110, 190, .6)'],
  [/brown|tan/i, 'rgba(170, 125, 80, .6)'],
  [/blue|navy/i, 'rgba(120, 150, 230, .55)'],
  [/white/i, 'rgba(255, 255, 255, .5)'],
  [/grey|gray|silver/i, 'rgba(200, 205, 215, .38)'],
];

/** A fabric swatch: the real photo when synced, otherwise a CSS weave in the fabric's colour and pattern. */
export function swatchAttrs(fabric, photo) {
  if (!fabric) return { cls: 'swatch swatch--solid', style: '--fab:#1c1f2a' };
  const base = validHex(fabric.color_hex) || '#1c1f2a';
  const desc = `${fabric.pattern || ''} ${fabric.pattern_desc || ''}`;
  let kind = 'solid';
  if (/stripe/i.test(desc)) kind = 'stripe';
  else if (/check|plaid|windowpane|grid|prince of wales/i.test(desc)) kind = 'check';
  const line = (STRIPE_WORDS.find(([re]) => re.test(fabric.pattern_desc || '')) || [null, 'rgba(255,255,255,.26)'])[1];
  let style = `--fab:${base};--line-c:${line}`;
  if (photo) style += `;background-image:url('${photo.replace(/'/g, '%27')}')`;
  return { cls: `swatch ${photo ? 'swatch--photo' : `swatch--${kind}`}`, style };
}

/** Split "Connor McDavid" -> ["Connor", "McDavid"] for two-line board cells. */
export function splitName(name) {
  const parts = String(name || '').trim().split(/\s+/);
  if (parts.length < 2) return ['', parts[0] || ''];
  return [parts[0], parts.slice(1).join(' ')];
}

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
export const titleCase = (s) => String(s || '').replace(/^\w/, (c) => c.toUpperCase());

/** Human label for suit option slugs: 'double-vent' -> 'Double vent', keeps '3-roll-2' and '5-button'. */
export function humanize(v) {
  if (v == null || v === '') return '';
  const s = String(v);
  if (/^\d/.test(s)) return s;
  return titleCase(s.replace(/-/g, ' '));
}

/* ------------------------------------------------------------------ time */
const leagueFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: CONFIG.tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
});
const localFmt = new Intl.DateTimeFormat('en-US', {
  weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
});
const timeFmt = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const dayFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

export const fmtLeague = (iso) => leagueFmt.format(new Date(iso));
/** 'Mon, Sep 28 · 8:00 AM MDT' in the league's time zone. */
export const fmtWhen = (iso) => fmtLeague(iso).replace(/, (\d{1,2}:)/, ' · $1');
export const fmtWhenLocal = (iso) => fmtLocal(iso).replace(/, (\d{1,2}:)/, ' · $1');
export const fmtLocal = (iso) => localFmt.format(new Date(iso));
export const fmtClock = (d) => timeFmt.format(d instanceof Date ? d : new Date(d));
export function fmtDay(isoDate) {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(isoDate) ? `${isoDate}T12:00:00Z` : isoDate);
  return Number.isNaN(d.getTime()) ? String(isoDate) : dayFmt.format(d);
}
export function ago(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return 'just now';
  if (s < 90) return '1 min ago';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
export function countdownParts(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return { d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600), m: Math.floor((s % 3600) / 60), s: s % 60 };
}
export const pad2 = (n) => String(n).padStart(2, '0');

/** Where the league is on its calendar right now. */
export function phase(at = now(), draftInfo = null) {
  const now_ = at;
  const start = Date.parse(CONFIG.draftStart);
  const drop = Date.parse(CONFIG.puckDrop);
  const end = Date.parse(CONFIG.seasonEnd);
  if (now_ < start) return 'pre-draft';
  if (draftInfo && !draftInfo.complete && now_ < drop + 86400000) return 'drafting';
  if (now_ < drop) return draftInfo?.complete ? 'post-draft' : 'drafting';
  if (now_ < end) return 'season';
  return 'final';
}

/** Minimal draft status from draft.json (or the manifest summary). */
export function draftInfo(league) {
  if (!league?.draft) return null;
  const total = league.total;
  const made = league.picks.length;
  return { orderSet: !!league.order, picks: made, total, complete: !!total && made >= total, started: made > 0 };
}

/* ------------------------------------------------------------------ report card (post-draft peer grades) */
export const GRADE_POINTS = { 'A+': 4.3, A: 4.0, 'A-': 3.7, 'B+': 3.3, B: 3.0, 'B-': 2.7, 'C+': 2.3, C: 2.0, 'C-': 1.7, D: 1.0, F: 0.0 };
const GRADES = Object.keys(GRADE_POINTS);
const num = (v) => (v != null && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null);

/** Nearest letter on the 4.3 scale; an exact midpoint rounds up. */
export function gpaLetter(gpa) {
  const x = num(gpa);
  if (x == null) return null;
  return GRADES.reduce((best, g) => (Math.abs(GRADE_POINTS[g] - x) < Math.abs(GRADE_POINTS[best] - x) - 1e-9 ? g : best), GRADES[0]);
}
export const gradeText = (g) => String(g || '').replace('-', '−');
export const gradeSpeech = (g) => String(g || '').replace('+', ' plus').replace('-', ' minus');
export const gradeTier = (g) => (GRADE_POINTS[g] != null ? g[0] : 'X');
export function ordinal(n) {
  const v = Math.round(Number(n));
  if (!Number.isFinite(v)) return '';
  const suffix = v % 100 >= 11 && v % 100 <= 13 ? 'th' : { 1: 'st', 2: 'nd', 3: 'rd' }[v % 10] || 'th';
  return `${v}${suffix}`;
}
/** Spiciest first: lowest grade, ties broken by the longer comment (as read on air, without delivery tags). */
export function spiciest(grades) {
  return [...(grades || [])].sort((a, b) => (GRADE_POINTS[a.grade] ?? 99) - (GRADE_POINTS[b.grade] ?? 99)
    || spoken(b.comment).length - spoken(a.comment).length);
}
export function spreadInfo(spread) {
  const s = num(spread);
  if (s == null) return null;
  if (s === 0) return { key: 'unanimous', label: 'Unanimous' };
  if (s <= 1.0) return { key: 'consensus', label: 'Consensus' };   // e.g. A+ to B+
  if (s <= 2.0) return { key: 'split', label: 'Split' };           // e.g. A to C
  return { key: 'divided', label: 'Room divided' };                // e.g. A+ to C, B+ to F
}

/**
 * grades.json -> report card model, or null until a team has actually been graded
 * (the export exists but is empty before the post-draft session runs).
 */
export function buildReport(doc) {
  const rows = (Array.isArray(doc?.teams) ? doc.teams : [])
    .filter((t) => t && t.team && Array.isArray(t.grades) && t.grades.length && num(t.gpa) != null)
    .sort((a, b) => num(b.gpa) - num(a.gpa) || String(a.team).localeCompare(String(b.team)));
  if (!rows.length) return null;
  const filers = Array.isArray(doc.projections) ? doc.projections.length : 0;
  const projections = new Map((doc.projections || []).filter((p) => p && p.team).map((p) => [p.team, p]));
  let rank = 0;
  const out = rows.map((r, i) => {
    if (i === 0 || num(r.gpa) < num(rows[i - 1].gpa)) rank = i + 1;
    const pts = r.grades.map((g) => GRADE_POINTS[g.grade]).filter((v) => v != null);
    const self = num(r.self_prediction);
    const avg = num(r.avg_predicted_finish);
    // league_predicted_finish is what everyone else thinks (exact); older exports only have avg_predicted_finish,
    // which includes the GM's own ranking, so back that out.
    const league = num(r.league_predicted_finish)
      ?? (avg != null && self != null && filers > 1 ? (avg * filers - self) / (filers - 1) : avg);
    return {
      ...r, rank, letter: gpaLetter(r.gpa), spread: spreadInfo(r.spread), self, league,
      high: pts.length ? GRADES.find((g) => GRADE_POINTS[g] === Math.max(...pts)) : null,
      low: pts.length ? GRADES.find((g) => GRADE_POINTS[g] === Math.min(...pts)) : null,
      projection: projections.get(r.team) || null,
    };
  });
  const filed = out.filter((r) => r.self != null);
  const gaps = filed.filter((r) => r.league != null).map((r) => ({ row: r, gap: Math.round(r.league) - r.self }))
    .sort((a, b) => b.gap - a.gap || a.row.self - b.row.self);
  return {
    rows: out,
    byTeam: new Map(out.map((r) => [r.team, r])),
    filed: filed.length,
    first: filed.filter((r) => r.self === 1).length,
    top3: filed.filter((r) => r.self <= 3).length,
    deluded: gaps[0] && gaps[0].gap > 0 ? gaps[0] : null,
    humble: gaps.length && gaps[gaps.length - 1].gap < 0 ? gaps[gaps.length - 1] : null,
  };
}

export const ICONS = {
  arrow: raw('<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M11.3 4.3a1 1 0 0 1 1.4 0l5 5a1 1 0 0 1 0 1.4l-5 5a1 1 0 1 1-1.4-1.4l3.3-3.3H3a1 1 0 1 1 0-2h11.6l-3.3-3.3a1 1 0 0 1 0-1.4z"/></svg>'),
  external: raw('<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M11 3a1 1 0 1 0 0 2h2.6l-6.3 6.3a1 1 0 1 0 1.4 1.4L15 6.4V9a1 1 0 1 0 2 0V4a1 1 0 0 0-1-1h-5z"/><path d="M5 5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-3a1 1 0 1 0-2 0v3H5V7h3a1 1 0 0 0 0-2H5z"/></svg>'),
  close: raw('<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M4.3 4.3a1 1 0 0 1 1.4 0L10 8.6l4.3-4.3a1 1 0 1 1 1.4 1.4L11.4 10l4.3 4.3a1 1 0 0 1-1.4 1.4L10 11.4l-4.3 4.3a1 1 0 0 1-1.4-1.4L8.6 10 4.3 5.7a1 1 0 0 1 0-1.4z"/></svg>'),
  play: raw('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.1v13.8a1 1 0 0 0 1.5.9l11-6.9a1 1 0 0 0 0-1.7l-11-6.9A1 1 0 0 0 8 5.1z"/></svg>'),
  info: raw('<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0zM9 9a1 1 0 0 0 0 2v3a1 1 0 0 0 1 1h1a1 1 0 1 0 0-2v-3a1 1 0 0 0-1-1H9z" clip-rule="evenodd"/></svg>'),
  table: raw('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M9 9v11M15 9v11"/></svg>'),
  film: raw('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3V9z" fill="currentColor"/></svg>'),
  trophy: raw('<svg viewBox="0 0 48 48" fill="currentColor" aria-hidden="true"><path d="M14 6h20v4h8v5c0 5.5-4 10-9.3 10.8A11 11 0 0 1 26 31.7V36h6v6H16v-6h6v-4.3a11 11 0 0 1-6.7-5.9C10 25 6 20.5 6 15v-5h8V6zm-4 8v1c0 3 1.9 5.6 4.6 6.6A11 11 0 0 1 14 18v-4h-4zm28 0h-4v4c0 1.2-.2 2.4-.6 3.6A7 7 0 0 0 38 15v-1z"/></svg>'),
};
