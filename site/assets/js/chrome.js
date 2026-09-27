// Page chrome shared by every page: mobile nav, LIVE badge on the Draft link, footer year.
import { CONFIG, loadManifest, now as clock } from './lib.js';

document.documentElement.classList.add('js');

const toggle = document.querySelector('.nav-toggle');
const nav = document.getElementById('site-nav');
if (toggle && nav) {
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    nav.classList.toggle('is-open', open);
  };
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && nav.classList.contains('is-open')) {
      setOpen(false);
      toggle.focus();
    }
  });
  document.addEventListener('click', (e) => {
    if (nav.classList.contains('is-open') && !nav.contains(e.target) && !toggle.contains(e.target)) setOpen(false);
  });
  matchMedia('(min-width: 900px)').addEventListener('change', (e) => e.matches && setOpen(false));
}

for (const el of document.querySelectorAll('[data-year]')) el.textContent = String(new Date().getFullYear());

// "LIVE" on the Draft link while the draft is running (per the last sync's summary).
loadManifest().then((m) => {
  const now = clock();
  const start = Date.parse(CONFIG.draftStart);
  const d = m?.draft;
  const live = now >= start && now < start + 2 * 86400000 && !!d && d.order_set && !d.complete;
  for (const el of document.querySelectorAll('[data-live-badge]')) el.hidden = !live;
});
