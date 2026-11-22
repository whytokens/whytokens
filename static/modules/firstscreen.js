// @ts-check
// First screen: one receipt before the dashboard. The biggest avoidable cause, where it happened
// (a link into Trace at that turn), the one change to try, and how sure we are. It only picks from
// the existing /api/waste payload: no detector logic lives here.
import { esc, fmtCostBig } from './format.js';

// Overhead that is the price of the work or of delegation is not "avoidable": the receipt skips it.
const NOT_AVOIDABLE = new Set(['large_context', 'subagent_preamble']);

/**
 * The receipt for a waste payload, or null when nothing counted is worth showing.
 * @param {{rollup?: any[], findings?: any[]}} waste
 */
export function pickReceipt(waste) {
  const top = (waste.rollup || []).find(c => c.in_total && !NOT_AVOIDABLE.has(c.cause) && c.usd > 0);
  if (!top) return null;
  const of = (waste.findings || []).filter(f => f.cause === top.cause && f.in_total !== false);
  // A finding with a next-step rule beats a bigger one without: the card must end in something to do.
  const best = [...of].sort((a, b) => (!!b.action - !!a.action) || b.usd - a.usd)[0];
  if (!best) return null;
  const call = (best.action ? best.action.calls : best.evidence || []).find(c => c.message_id) || null;
  return { cause: top.cause, usd: top.usd, sessions: top.sessions, tier: top.tier, finding: best, call };
}

/** HTML of the card. `label` names the cause; `avoid` is its avoidable tag HTML. */
export function receiptHTML(r, label, avoid) {
  const f = r.finding, a = f.action;
  const sid = esc(f.session_id), turn = r.call ? esc(r.call.message_id) : '';
  const why = esc((label + ': ' + f.detail).slice(0, 200));
  const where = r.call
    ? `<button class="filter-btn" data-session="${sid}" data-turn="${turn}" data-why="${why}"` +
      ` onclick="openSessionDetail(this.dataset.session, this.dataset.turn, {turn: this.dataset.turn, why: this.dataset.why})">Open this turn in the trace</button>`
    : `<button class="filter-btn" data-session="${sid}" onclick="openSessionDetail(this.dataset.session)">Open this session</button>`;
  return `<div class="fs-head"><span class="fs-kicker">The one thing to try first</span>` +
    `<button class="link-btn fs-dismiss" type="button" data-fs-dismiss>Hide</button></div>` +
    `<p class="fs-cause"><strong>${esc(label)}</strong> ${avoid} cost about <strong>${fmtCostBig(r.usd)}</strong>` +
    ` across ${r.sessions} session${r.sessions === 1 ? '' : 's'}, all history <span class="muted">(API list rate, lower bound, not savings)</span></p>` +
    `<p class="fs-where"><span class="muted">Where:</span> ${esc(f.detail)} ${where}</p>` +
    `<p class="fs-try"><span class="muted">Try:</span> ${a && a.rule ? `<code>${esc(a.rule)}</code>` : esc(f.fix)}</p>` +
    `<p class="fs-tier muted">How sure: <strong>${esc(r.tier || f.tier)}</strong>. Estimated from the transcripts, not billed by the vendor.</p>`;
}
