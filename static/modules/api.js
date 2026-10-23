// @ts-check
// Thin fetch wrappers around the local server's read-API (api.py). Each returns
// parsed JSON; callers handle any `{error}` payload the server may return.

/** GET /api/data — full-history snapshot the client filters/aggregates.
 * `tz` ('local' | 'utc') picks the calendar the server buckets every `day`
 * field into; it must match the calendar the client derives range bounds in. */
export async function fetchData(tz = 'local') {
  const resp = await fetch('/api/data?tz=' + encodeURIComponent(tz));
  return resp.json();
}

/** GET /api/session?session_id=… — per-session drilldown. */
export async function fetchSession(sessionId) {
  const resp = await fetch('/api/session?session_id=' + encodeURIComponent(sessionId));
  return resp.json();
}

/**
 * GET /api/session-breakdown?session_id=… — per-actor token split for one session
 * (main thread vs each subagent type / skill). Tokens only; the caller prices them
 * with the editable pricing table.
 */
export async function fetchSessionBreakdown(sessionId) {
  const resp = await fetch('/api/session-breakdown?session_id=' + encodeURIComponent(sessionId));
  return resp.json();
}

/**
 * GET /api/sessions — paginated, SQL-side-filtered session list (hub endpoint).
 * The dashboard itself still uses the full /api/data snapshot; this
 * wrapper exists for external callers and future server-side paging.
 * @param {Record<string, string|number>} [params] project/model/start/end/limit/offset/sort
 */
export async function fetchSessions(params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v != null)
      .map(([k, v]) => [k, String(v)])
  ).toString();
  const resp = await fetch('/api/sessions' + (qs ? '?' + qs : ''));
  return resp.json();
}

/**
 * GET /api/daily — SQL-side daily token aggregation with server-computed cost.
 * @param {Record<string, string>} [params] start/end/model/project
 */
export async function fetchDaily(params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v != null)
  ).toString();
  const resp = await fetch('/api/daily' + (qs ? '?' + qs : ''));
  return resp.json();
}

/** GET /api/extensions — tabs supplied by loaded extensions: {tabs:[{name,label,module_url}]}. */
export async function fetchExtensions() {
  return fetchJson('/api/extensions');
}

/** GET any same-origin JSON path (used by extension tabs). */
export async function fetchJson(path) {
  const resp = await fetch(path);
  return resp.json();
}

/** GET /api/pricing — the single-source price table (pricing.py). */
export async function fetchPricing() {
  const resp = await fetch('/api/pricing');
  return resp.json();
}

/** POST /api/rescan — destructive full DB rebuild across all sources (Claude +
 * Codex). Returns flat totals {new, updated, skipped, turns, sessions} summed
 * over sources, plus a per-source breakdown under .sources. */
export async function postRescan() {
  const resp = await fetch('/api/rescan', {
    method: 'POST',
    headers: { 'X-Whytokens-Token': document.querySelector('meta[name=whytokens-token]')?.content || '' },
  });
  return resp.json();
}

/** GET /api/waste — findings, per-cause rollup, measurement status (API list rate). */
export async function fetchWaste() {
  const resp = await fetch('/api/waste');
  return resp.json();
}

/** GET /api/status — discovered roots, scan progress, empty-state hint. */
export async function fetchStatus() {
  const resp = await fetch('/api/status');
  return resp.json();
}
