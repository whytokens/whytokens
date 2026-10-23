// @ts-check
// Pure formatting/escaping helpers. No DOM mutation, no app state.

/**
 * HTML-escape a value for safe innerHTML interpolation (XSS protection).
 * @param {*} s
 * @returns {string}
 */
export function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  // quotes too: callers interpolate into attribute values (data-session="...")
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Compact token count: 1.50M / 1.5K / 999.
 * @param {number} n
 * @returns {string}
 */
export function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}

/** Cost to 4 decimals ($0.0000). @param {number} c @returns {string} */
export function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
/** Cost to 2 decimals ($0.00), for headline totals. @param {number} c @returns {string} */
export function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
/** Fraction as a percentage (0.0% when non-finite). @param {number} v @returns {string} */
export function fmtPct(v)     { return isFinite(v) ? (v * 100).toFixed(1) + '%' : '0.0%'; }

/**
 * ISO-UTC timestamp → "YYYY-MM-DD HH:MM" in the display timezone.
 * Timestamps are stored UTC (…Z); rendering the raw slice shows UTC, so parse
 * and reformat. Falls back to a plain slice if the value won't parse; "" → "".
 * @param {string} ts
 * @param {string} [tz] 'local' (default) or 'utc'
 * @returns {string}
 */
export function fmtLocalDateTime(ts, tz = 'local') {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts).slice(0, 16).replace('T', ' ');
  if (tz === 'utc') return d.toISOString().slice(0, 16).replace('T', ' ');
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
