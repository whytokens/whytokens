// Overview budget band: this calendar month against the monthly budget, from GET /api/budget.
// Every figure is computed server-side (budget.py); this only words them.
import { esc, fmtCostBig } from './format.js';

export const BASIS = 'estimated at API list price';
export const METHOD_TEXT = {
  trailing_7d: 'Projection = spent + trailing 7-day daily average x days remaining',
  month_to_date: 'Projection = spent + month-to-date daily average x days remaining (month-to-date average: fewer than 7 days of data)',
};

const cell = (label, value, cls = '') => `<div class="budget-cell"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>`;

/** The band's HTML. `m` is the /api/budget payload (null when it could not load);
 * `links` = { sessions, waste, settings } hrefs built by the caller from the URL-state helpers. */
export function budgetBandHTML(m, links) {
  if (!m || m.error) return '';
  const method = METHOD_TEXT[m.method] || '';
  const over = m.gap != null && m.gap > 0;
  let verdict = '', needed = '—';
  if (m.budget != null) {
    verdict = over ? `Projected to exceed the budget by ${fmtCostBig(m.gap)}.` : `Projected to finish ${fmtCostBig(-m.gap)} under the budget.`;
    if (m.needed_daily != null) needed = m.needed_daily > 0 ? `${fmtCostBig(m.needed_daily)}/day` : 'Budget already spent';
  }
  const cells = [cell('Spent this month', fmtCostBig(m.spent)),
    cell('Budget', m.budget == null ? 'Not set' : fmtCostBig(m.budget)),
    cell('Projected month-end', fmtCostBig(m.projected), over ? 'budget-over' : '')];
  if (m.budget != null) cells.push(cell(over ? 'Over by' : 'Under by', fmtCostBig(Math.abs(m.gap)), over ? 'budget-over' : 'budget-under'),
    cell('Needed daily rate', needed));
  return `<div class="budget-head"><span class="budget-title">Monthly budget · ${esc(m.month)}</span> <span class="muted">${BASIS}</span></div>` +
    `<div class="budget-grid">${cells.join('')}</div>` +
    (verdict ? `<div class="budget-verdict${over ? ' budget-over' : ''}" role="status">${esc(verdict)}</div>` : '') +
    `<div class="budget-sub muted" title="${esc(method)}">${esc(method)}</div>` +
    `<div class="budget-links"><a data-drill href="${esc(links.sessions)}">Sessions this month by cost</a>` +
    (over ? ` <a data-drill href="${esc(links.waste)}">Where to act (Waste)</a>` : '') +
    (m.budget == null ? ` <a data-drill href="${esc(links.settings)}">Set a budget</a>` : '') + '</div>';
}
