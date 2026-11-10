// @ts-check
// Provenance badges. The vocabulary + tooltips come from GET /api/provenance
// (provenance.py); nothing here names them. Tag a card label or column header with
// data-prov="official|local_estimate|unknown" and the observer appends the badge.
import { esc, fmt } from './format.js';

/** @type {Record<string,string>} */
let LABELS = {};
export function setProvenance(labels) { LABELS = labels || {}; }

/** @param {string} kind a key of provenance.LABELS @returns {string} badge HTML */
export function provBadge(kind) {
  return `<span class="prov prov-${esc(kind)}" title="${esc(LABELS[kind] || kind)}">${esc(kind.replace('_', ' '))}</span>`;
}

/** Add f.usd to acc[f.attribution] (a waste cause's observed / inferred halves). */
export function addSplit(acc, f) { acc[f.attribution] += f.usd; }

/** A value that may not be measurable renders as a dash, never 0. */
export function orUnknown(value, unknown, format = fmt) {
  return unknown || value == null ? '—' : format(value);
}

/** Fetch the vocabulary, then badge every [data-prov] node now and as the UI re-renders. */
export async function watchProvenance() {
  try { setProvenance(await (await fetch('/api/provenance')).json()); } catch (e) { /* badges fall back to the bare key */ }
  const decorate = () => {
    for (const el of document.querySelectorAll('[data-prov]:not([data-prov-done])')) {
      el.setAttribute('data-prov-done', '1');
      el.insertAdjacentHTML('beforeend', ' ' + provBadge(/** @type {string} */ (el.getAttribute('data-prov'))));
    }
  };
  decorate();
  new MutationObserver(decorate).observe(document.body, { childList: true, subtree: true });
}
