// Screen dispatch: the six rail screens, legacy-id redirects, rail expand/collapse
// and the extension-tab seam. Rendering stays in app.js (renderTab); this module
// only knows ids, visibility and the rail DOM.
import { store, RAIL_KEY } from './theme.js';

// Rail order. Extension tabs are pushed after these by app.js loadExtensionTabs().
export const VALID_TABS = ['overview', 'sessions', 'trace', 'waste', 'sources', 'settings'];
// Folded screens (mapping.md): old ?tab= ids land on a section of a new screen.
export const LEGACY_TABS = {
  projects: { tab: 'overview', hash: 'projects' },
  tools: { tab: 'overview', hash: 'tools' },
  pricing: { tab: 'settings', hash: 'pricing' },
};
// Screens that ignore the filter bar hide it.
export const NO_FILTER_TABS = ['settings', 'sources'];

/** ?tab= value (+ current hash) -> { tab, hash } with legacy ids redirected. */
export function resolveTab(param, hash) {
  if (Object.hasOwn(LEGACY_TABS, param)) return LEGACY_TABS[param];
  return { tab: VALID_TABS.includes(param) ? param : 'overview', hash: (hash || '').replace(/^#/, '') };
}

export function applyTabVisibility(name) {
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b => {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tab-panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
  document.getElementById('filter-bar').hidden = NO_FILTER_TABS.includes(name);
}

/** Rail button for an extension tab, placed after the built-ins, before the expand arrow. */
export function addExtRailItem(t) {
  const btn = document.createElement('button');
  btn.className = 'tab-btn';
  btn.id = 'tab-' + t.name;
  btn.dataset.tab = t.name;
  btn.setAttribute('aria-controls', 'panel-' + t.name);
  btn.setAttribute('aria-label', t.label);
  btn.title = t.label;
  btn.innerHTML = '<span class="ico" aria-hidden="true">&#9671;</span><span class="lbl"></span>';
  btn.querySelector('.lbl').textContent = t.label;
  document.getElementById('tab-bar').insertBefore(btn, document.querySelector('#tab-bar .rail-spacer'));
  const sec = document.createElement('section');
  sec.className = 'tab-panel';
  sec.id = 'panel-' + t.name;
  sec.dataset.panel = t.name;
  sec.hidden = true;
  document.getElementById('dashboard-view').appendChild(sec);
}

function paintRail(expanded) {
  const rail = document.getElementById('tab-bar');
  const btn = document.getElementById('rail-toggle');
  rail.classList.toggle('expanded', expanded);
  btn.setAttribute('aria-expanded', String(expanded));
  btn.title = expanded ? 'Collapse navigation' : 'Expand navigation';
  document.getElementById('rail-arrow').innerHTML = expanded ? '&#8249;' : '&#8250;';
}

export function toggleRail() {
  const expanded = document.getElementById('rail-toggle').getAttribute('aria-expanded') !== 'true';
  paintRail(expanded);
  if (expanded) store.set(RAIL_KEY, 'expanded'); else store.remove(RAIL_KEY);
}

export function initRail() { paintRail(store.get(RAIL_KEY) === 'expanded'); }
