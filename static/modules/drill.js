// Drilldown navigation: the URL <-> Sessions/Waste state mapping, the links the Overview cards use,
// and the per-session overhead index. Pure (no DOM), so tests/test_drilldown.py runs it under node.
// Dollars here are the server's (waste.py findings, list rate); this module only sums and orders them.

export const SESSION_SORTS = ['last', 'duration_min', 'turns', 'input', 'output', 'cache_read',
  'cache_creation', 'cost', 'cc_cost', 'cache_share', 'overhead', 'peers', 'shape'];
export const DEFAULT_SORT = { col: 'last', dir: 'desc' };
export const EMPTY_FILTERS = { project: '', branch: '', tool: '', minCost: 0, hasCacheCreation: false, cause: '',
  shape: '', tag: '', minX: 0 };

/** Sessions-list state -> URL params. Defaults are omitted. */
export function writeSessionState(params, f = EMPTY_FILTERS, sort = DEFAULT_SORT) {
  if (f.project) params.set('sproject', f.project);
  if (f.branch) params.set('sbranch', f.branch);
  if (f.tool) params.set('stool', f.tool);
  if (f.minCost) params.set('smin', String(f.minCost));
  if (f.hasCacheCreation) params.set('scache', '1');
  if (f.cause) params.set('cause', f.cause);
  if (f.shape) params.set('sshape', f.shape);
  if (f.tag) params.set('stag', f.tag);
  if (f.minX) params.set('sx', String(f.minX));
  if (sort.col !== DEFAULT_SORT.col || sort.dir !== DEFAULT_SORT.dir) { params.set('sort', sort.col); params.set('dir', sort.dir); }
}

/** ?search -> { filters, sort }. Unknown sort columns and directions fall back to the default. */
export function readSessionState(search) {
  const q = new URLSearchParams(search);
  const col = q.get('sort'), dir = q.get('dir');
  const sort = SESSION_SORTS.includes(col)
    ? { col, dir: dir === 'asc' ? 'asc' : 'desc' } : { ...DEFAULT_SORT };
  return {
    filters: {
      project: q.get('sproject') || '', branch: q.get('sbranch') || '', tool: q.get('stool') || '',
      minCost: parseFloat(q.get('smin')) || 0, hasCacheCreation: q.get('scache') === '1', cause: q.get('cause') || '',
      shape: q.get('sshape') || '', tag: q.get('stag') || '', minX: parseFloat(q.get('sx')) || 0,
    },
    sort,
  };
}

/** Waste-tab selection: the cause bar and the heatmap week. */
export function writeWasteState(params, cause, week) {
  if (cause) params.set('cause', cause);
  if (week) params.set('week', week);
}
export function readWasteState(search) {
  const q = new URLSearchParams(search);
  return { cause: q.get('cause') || '', week: /^\d{4}-\d{2}-\d{2}$/.test(q.get('week') || '') ? q.get('week') : '' };
}

/** A drilldown URL: `globals` (range, tz, models) plus the target screen's own state. */
export function drillHref(globals, { tab, filters, sort, cause, week }) {
  const p = new URLSearchParams(globals);
  if (tab && tab !== 'overview') p.set('tab', tab);
  if (tab === 'sessions') writeSessionState(p, { ...EMPTY_FILTERS, ...filters }, sort || DEFAULT_SORT);
  if (tab === 'waste') writeWasteState(p, cause, week);
  const s = p.toString();
  return s ? '?' + s : '?';
}

/** The Sessions view that explains each Overview card (the Overhead card and the cause rows go to Waste). */
export const CARD_TARGET = {
  'Est. Cost': { col: 'cost', dir: 'desc' },
  'Uncached': { col: 'input', dir: 'desc' },
  'Output': { col: 'output', dir: 'desc' },
  'Cache Read': { col: 'cache_share', dir: 'asc' },
  'Cache Write': { col: 'cache_creation', dir: 'desc' },
  'Cache Savings': { col: 'cache_share', dir: 'asc' },
  'Sessions': DEFAULT_SORT,
};

/** False for observation findings (waste.UNCOUNTED): shown and filterable, never summed into a total. */
export const counted = f => f.in_total !== false;

/** findings -> Map(session_id -> { total, byCause }). total holds counted findings only. */
export function overheadIndex(findings) {
  const idx = new Map();
  for (const f of findings || []) {
    const e = idx.get(f.session_id) || idx.set(f.session_id, { total: 0, byCause: {} }).get(f.session_id);
    if (counted(f)) e.total += f.usd;
    e.byCause[f.cause] = (e.byCause[f.cause] || 0) + f.usd;
  }
  return idx;
}

/** A session's attributed overhead: the selected cause's share, or all causes. */
export function overheadOf(idx, sessionId, cause = '') {
  const e = idx && idx.get(sessionId);
  return e ? (cause ? e.byCause[cause] || 0 : e.total) : 0;
}
