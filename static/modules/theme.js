// Theme preference + the ONE chart colour token map.
//
// Every colour lives in styles.css as a CSS custom property (light, dark and the
// prefers-color-scheme media block). Charts never hold a literal: fillTokens()
// reads the live computed values at render time, so a theme change only needs a
// re-read plus a redraw. Storage is optional: every access is guarded, and the
// page behaves (without persistence) when localStorage is blocked.

const THEME_KEY = 'whytokens.theme';   // 'light' | 'dark'; absent = follow the OS
const RAIL_KEY = 'whytokens.rail';     // 'expanded'; absent = icon-only
const TZ_KEY = 'whytokens.tz';         // default display timezone: 'local' | 'utc'

export const store = {
  get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
  set(key, val) { try { window.localStorage.setItem(key, val); } catch (e) { /* storage blocked */ } },
  remove(key) { try { window.localStorage.removeItem(key); } catch (e) { /* storage blocked */ } },
};
export { RAIL_KEY, TZ_KEY };

// Chart token names -> CSS variable `--<name>`. The single map; add a colour here
// and in styles.css (both themes), nowhere else.
const BASE = ['text', 'muted', 'axis', 'border', 'card', 'blue', 'green', 'red', 'accent', 'amber', 'purple', 'teal', 'mauve'];
const TOKENS = ['input', 'output', 'cache_read', 'cache_creation'];  // --tok-<name>
const CATEGORICAL = 8;                                               // --cat-1 .. --cat-8
export const TOKEN_MAP = { BASE, TOKENS, CATEGORICAL };

/** "#rrggbb" + alpha -> "rgba(...)". Non-hex input is returned unchanged. */
export function alpha(color, a) {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
}

/** Refill the (mutable) token objects from the computed CSS. Safe to call every render. */
export function fillTokens(C, TOKEN_COLORS, TOKEN_HOVER, MODEL_COLORS) {
  const cs = getComputedStyle(document.documentElement);
  const v = name => cs.getPropertyValue('--' + name).trim();
  for (const k of BASE) C[k] = v(k);
  for (const k of TOKENS) {
    const solid = v('tok-' + k);
    TOKEN_COLORS[k] = alpha(solid, k === 'input' || k === 'output' ? 0.85 : 0.75);
    TOKEN_HOVER[k] = solid;
  }
  MODEL_COLORS.length = 0;
  for (let i = 1; i <= CATEGORICAL; i++) MODEL_COLORS.push(v('cat-' + i));
}

let onChange = () => {};

let themePref = null;  // in-memory copy, so the UI stays right when storage is unavailable
export function getThemePref() {
  const t = themePref ?? store.get(THEME_KEY);
  return t === 'light' || t === 'dark' ? t : 'system';
}

function syncThemeButtons() {
  const pref = getThemePref();
  document.querySelectorAll('[data-theme-opt]').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.themeOpt === pref)));
}

export function setThemePref(pref) {
  const root = document.documentElement;
  themePref = pref === 'light' || pref === 'dark' ? pref : 'system';
  if (pref === 'light' || pref === 'dark') { root.dataset.theme = pref; store.set(THEME_KEY, pref); }
  else { delete root.dataset.theme; store.remove(THEME_KEY); }
  syncThemeButtons();
  onChange();
}

/** Apply the saved theme, track OS changes, and call cb whenever the palette may have changed. */
export function initTheme(cb) {
  onChange = cb;
  const pref = getThemePref();
  if (pref !== 'system') document.documentElement.dataset.theme = pref;
  syncThemeButtons();
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (getThemePref() === 'system') onChange();
    });
  } catch (e) { /* no matchMedia */ }
}
