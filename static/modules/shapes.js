// @ts-check
// Work shape per session and peer baselines: the Sessions columns/filters, the Trace line, the
// Overview outlier line and by-shape table, and the project tag editor. Data and every number come
// from GET /api/shapes (shape.py); this module only draws them. The pure helpers need no DOM.
import { esc, fmtCostBig, fmtPct } from './format.js';
import { postProjectTag } from './api.js';

const UNKNOWN = 'not derived yet';

/** Display label of a shape key ("unknown" = shapes not synced yet). */
export const shapeLabel = (d, shape) => shape === 'unknown' || !shape ? UNKNOWN : (d && d.labels && d.labels[shape]) || shape;

/** This session's entry in the payload, or null. */
export const shapeOf = (d, id) => (d && d.sessions && d.sessions[id]) || null;

/** Multiple of the peer median, or null when none. */
export const peersOf = (d, id) => { const e = shapeOf(d, id); return e && e.x != null ? e.x : null; };

export const fmtX = x => x.toFixed(1) + 'x';

/** "2.4x the median for edit-heavy sessions in this project, n=18"; '' when there is no multiple. */
export function baselineSentence(entry, labels) {
  if (!entry || entry.x == null) return '';
  const who = entry.basis === 'project' ? 'sessions of any shape'
    : `${(labels && labels[entry.shape]) || entry.shape} sessions`;
  return `${fmtX(entry.x)} the median for ${who} in this project, n=${entry.n}`;
}

/** Does a Sessions row pass the shape / tag / min-multiple filters? Unknown payload keeps every row. */
export function shapeKeeps(d, s, f) {
  if (!d) return true;
  const e = shapeOf(d, s.full_session_id);
  if (f.shape && !(f.shape === 'unknown' ? e && (!e.shape || e.shape === 'unknown') : e && e.shape === f.shape)) return false;  // 'unknown' = the NULL-shape sessions the By shape row counts
  if (f.tag && (d.tags || {})[s.project] !== f.tag) return false;
  if (f.minX && !(e && e.x != null && e.x >= f.minX)) return false;
  return true;
}

/** Sessions "Shape" cell. */
export function shapeCell(d, id) {
  if (!d) return '<td class="muted">…</td>';
  const e = shapeOf(d, id);
  if (!e) return '<td class="muted">–</td>';
  return `<td class="shp-shape" title="${esc([shapeLabel(d, e.shape), e.detail].filter(Boolean).join(' '))}">${esc(shapeLabel(d, e.shape))}` +
    (e.detail ? ` <span class="muted"${e.detail_id ? ` title="${esc(e.detail + ' (' + e.detail_id + ')')}"` : ''}>${esc(e.detail)}</span>` : '') + '</td>';
}

/** Sessions "vs peers" cell; outliers get shp-outlier. */
export function peersCell(d, id) {
  if (!d) return '<td class="num muted">…</td>';
  const e = shapeOf(d, id);
  if (!e || e.x == null) return '<td class="num muted">–</td>';
  const out = e.x >= ((d.constants && d.constants.outlier_x) || 2);
  return `<td class="num${out ? ' shp-outlier' : ''}" title="${esc(baselineSentence(e, d.labels))}">${fmtX(e.x)}</td>`;
}

/** Trace header line (empty when there is no multiple). */
export function traceLineHtml(d, id) {
  const s = baselineSentence(shapeOf(d, id), d && d.labels);
  return s ? `<div class="shp-trace muted">${esc(s)}</div>` : '';
}

/** Overview line. `href` is the drilldown link; nothing when no session is an outlier. */
export function outlierLineHtml(d, href) {
  if (!d || !d.outliers) return '';
  const x = (d.constants && d.constants.outlier_x) || 2;
  return `<a class="shp-outliers" data-drill href="${esc(href)}">${d.outliers} session${d.outliers === 1 ? '' : 's'} cost ${x}x+ their peers</a>`;
}

/** Overview "By work shape" table; hrefOf(shape) -> Sessions link. */
export function shapeTableHtml(d, hrefOf) {
  if (!d || !d.rows || !d.rows.length) return '<p class="muted">No sessions in the database yet.</p>';
  return '<table class="shp-table"><thead><tr><th>Shape</th><th class="num">Sessions</th><th class="num">Cost</th>' +
    '<th class="num">Share of cost</th><th class="num" title="Attributed overhead as a share of this shape\'s cost (lower bound)">Overhead share</th></tr></thead><tbody>' +
    d.rows.map(r => `<tr><td><a data-drill href="${esc(hrefOf(r.shape))}">${esc(shapeLabel(d, r.shape))}</a></td>` +
      `<td class="num">${r.sessions}</td><td class="num">${fmtCostBig(r.usd)}</td>` +
      `<td class="num">${fmtPct(r.share)}</td><td class="num">${fmtPct(r.overhead_share)}</td></tr>`).join('') +
    '</tbody></table>';
}

/** Fill the Sessions shape/tag selects from the payload, keeping the current value selectable. */
export function fillShapeFilters(d) {
  if (!d || typeof document === 'undefined') return;
  const fill = (id, first, pairs) => {
    const sel = /** @type {HTMLSelectElement|null} */ (document.getElementById(id));
    if (!sel) return;
    const cur = sel.value;
    if (cur && !pairs.some(([v]) => v === cur)) pairs.push([cur, cur]);
    sel.innerHTML = `<option value="">${first}</option>` + pairs.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join('');
    sel.value = cur;
  };
  fill('session-shape-filter', 'Any shape', Object.entries(d.labels || {}).map(([k, v]) => [k, String(v)]));
  fill('session-tag-filter', 'Any tag', [...new Set(Object.values(d.tags || {}))].sort().map(t => [String(t), String(t)]));
}

/** Set a select's value even before its options are loaded (adds a stopgap option). */
export function setSelectValue(id, v) {
  const sel = /** @type {HTMLSelectElement} */ (document.getElementById(id));
  if (v && ![...sel.options].some(o => o.value === v)) sel.add(new Option(v, v));
  sel.value = v;
}

/** The Projects-table tag cell content: the tag (or a muted dash) and an edit button. */
export function tagCellHtml(project, tag) {
  return `<span class="${tag ? '' : 'muted'}">${tag ? esc(tag) : '–'}</span> ` +
    `<button class="link-btn shp-tag-btn" type="button" data-tag-project="${esc(project)}" aria-label="Edit tag of ${esc(project)}">edit</button>`;
}

/** Inline edit in a tag cell: Enter saves (empty clears), Esc cancels, a server 400 text stays visible. */
export function editTag(cell, project, tag, onSaved) {
  const restore = t => { cell.innerHTML = tagCellHtml(project, t); };
  cell.innerHTML = `<input class="shp-tag-input" type="text" maxlength="80" aria-label="Tag of ${esc(project)}" value="${esc(tag || '')}">` +
    '<span class="shp-tag-err" role="alert"></span>';
  const input = /** @type {HTMLInputElement} */ (cell.querySelector('input')), err = cell.querySelector('.shp-tag-err');
  input.focus(); input.select();
  let busy = false;
  input.addEventListener('keydown', async e => {
    if (e.key === 'Escape') { restore(tag); cell.querySelector('button')?.focus(); return; }
    if (e.key !== 'Enter' || busy) return;
    e.preventDefault(); busy = true;
    const v = input.value.trim();
    try { await postProjectTag(project, v || null); restore(v || null); onSaved(); cell.querySelector('button')?.focus(); }
    catch (x) { err.textContent = ' ' + x.message; busy = false; }
  });
}
