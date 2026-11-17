// Model what-if wording. Observation only: the server reprices the same tokens (whatif.py,
// GET /api/whatif); nothing here holds a price, and none of it feeds overhead or any total.
import { esc, fmtCostBig } from './format.js';

export const WHATIF_WORDING = 'Repricing only. It does not say the cheaper model would have done the work.';

/** The "If on <next tier down>" cell of the Cost by Model table; `row` is one by_model entry or undefined. */
export function whatifCellHTML(row) {
  if (!row || !row.to || row.delta_usd == null) return '<td class="num muted" title="No cheaper tier below this model">—</td>';
  const d = row.delta_usd;
  const tip = `On ${row.to}: ${fmtCostBig(row.repriced_usd)} (was ${fmtCostBig(row.actual_usd)}). ${WHATIF_WORDING}`;
  return `<td class="num whatif-cell" title="${esc(tip)}">${d < 0 ? '−' : '+'}${fmtCostBig(Math.abs(d))} <span class="muted">${esc(row.to)}</span></td>`;
}

/** One visible line for a lane group (subagent lanes of a session); '' when nothing can be repriced. */
export function lanesLineHTML(res) {
  const priced = res && !res.error ? (res.by_model || []).filter(m => m.to) : [];
  if (!priced.length) return '';
  const targets = [...new Set(priced.map(m => m.to))].join(', ');
  return `<span class="whatif-line">These lanes repriced on ${esc(targets)}: ${fmtCostBig(res.repriced_usd)} (was ${fmtCostBig(res.actual_usd)}).</span> ` +
    `<span class="muted">${esc(WHATIF_WORDING)}</span>`;
}
