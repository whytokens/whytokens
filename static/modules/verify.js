// Verify slot for a Waste habit (project, cause): before/after share of spend for a recorded
// fix (fixes.py via /api/fixes). Recording is CLI-only: `whytokens fix record`.
import { esc, fmtCostBig } from './format.js';

let fixesPromise = null;
export function resetVerify() { fixesPromise = null; }

/** The record-a-fix hint with this habit's real project and cause filled in. */
export function recordHint(project, cause) {
  return `whytokens fix record --project '${project.replace(/'/g, "''")}' --cause ${cause} --note "what you changed"`;
}

export function verifySlot(project, cause) {
  return `<div class="wg-verify muted" data-vproject="${esc(project)}" data-vcause="${esc(cause)}"></div>`;
}

const side = s => `${s.sessions} sessions, ${fmtCostBig(s.cause_usd)} of ${fmtCostBig(s.usd)} = ${(s.pct * 100).toFixed(1)}%`;

function describe(r) {
  const f = r.fix;
  let h = `<p><strong>Verify</strong> fix #${f.id} (${esc(f.fix_date)}): ${esc(f.note)}<br>` +
    `before: ${side(r.before)}<br>after: ${side(r.after)}<br><em>${esc(r.status)}</em>`;
  if (r.reason) h += `: ${esc(r.reason)}`;
  if (r.status === 'reduced') h += `: about ${fmtCostBig(r.saving_usd)} less over the after period (${esc(r.basis)})`;
  return h + `<br>${esc(r.caveat)}</p>`;
}

export async function fillVerifySlots(root) {
  const slots = root.querySelectorAll('.wg-verify');
  if (!slots.length) return;
  if (!fixesPromise) fixesPromise = fetch('/api/fixes').then(r => r.json()).catch(() => ({ results: [] }));
  const { results } = await fixesPromise;
  for (const slot of slots) {
    const mine = results.filter(r => r.fix.project === slot.dataset.vproject && r.fix.cause === slot.dataset.vcause);
    slot.innerHTML = mine.length ? mine.map(describe).join('')
      : `<p>No fix recorded. After changing this habit: <code>${esc(recordHint(slot.dataset.vproject, slot.dataset.vcause))}</code></p>`;
  }
}
