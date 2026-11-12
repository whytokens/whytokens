// Verify slot for a Waste habit (project, cause): before/after share of spend for a recorded
// fix (fixes.py via /api/fixes). Recording: the card's button (POST /api/fixes) or `whytokens fix record`.
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

/** The card's "I made this change" button; the note recorded is the rule title. */
export function recordButton(project, cause, note) {
  return `<button class="filter-btn wg-record" data-rproject="${esc(project)}" data-rcause="${esc(cause)}" data-rnote="${esc(note)}"` +
    ` onclick="recordFix(this)" title="Record today as the date you changed this habit, so the dashboard can compare before and after">I made this change</button>`;
}

// The server records a (project, cause, note) once, so a repeat click is harmless: this only
// keeps the button from double-firing while its own request is in flight.
export async function recordFix(btn) {
  btn.disabled = true;
  try {
    const resp = await fetch('/api/fixes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Whytokens-Token': document.querySelector('meta[name=whytokens-token]')?.content || '' },
      body: JSON.stringify({ project: btn.dataset.rproject, cause: btn.dataset.rcause, note: btn.dataset.rnote }),
    });
    if (!resp.ok) throw new Error(String(resp.status));
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Could not record, try again';
    return;
  }
  btn.outerHTML = '<span class="muted">Recorded</span>';
  resetVerify();
  fillVerifySlots(document);
}

// S4 while the after side is short of the minimum; S5 otherwise (today's before/after text).
function describe(r) {
  const f = r.fix, min = r.min_sessions || 10;
  if (r.status === 'insufficient data' && r.after.sessions < min)
    return `<p><strong>Recorded ${esc(f.fix_date)}:</strong> ${esc(f.note)}. ${r.after.sessions} of ${min} sessions since. Check back.</p>`;
  let h = `<p><strong>Verify</strong> fix #${f.id} (${esc(f.fix_date)}): ${esc(f.note)}<br>` +
    `before: ${side(r.before)}<br>after: ${side(r.after)}<br><em>${esc(r.status)}</em>`;
  if (r.reason) h += `: ${esc(r.reason)}`;
  if (r.status === 'no material change') h += ': try the next call on the list';
  if (r.status === 'reduced') h += `: about ${fmtCostBig(r.saving_usd)} less over the after period (${esc(r.basis)})`;
  return h + `<br>${esc(r.caveat)}</p>`;
}

export async function fillVerifySlots(root) {
  const slots = root.querySelectorAll('.wg-verify');
  if (!slots.length) return;
  if (!fixesPromise) fixesPromise = fetch('/api/fixes').then(r => r.json()).catch(() => { fixesPromise = null; return { results: [] }; });
  const { results } = await fixesPromise;
  const done = new Set();
  for (const r of results) done.add(r.fix.project + '|' + r.fix.cause + '|' + r.fix.note);
  for (const b of root.querySelectorAll('.wg-record'))
    if (done.has(b.dataset.rproject + '|' + b.dataset.rcause + '|' + b.dataset.rnote)) b.outerHTML = '<span class="muted">Recorded</span>';
  for (const slot of slots) {
    const mine = results.filter(r => r.fix.project === slot.dataset.vproject && r.fix.cause === slot.dataset.vcause);
    slot.innerHTML = mine.length ? mine.map(describe).join('')
      : `<p>No fix recorded yet. From a terminal instead: <code>${esc(recordHint(slot.dataset.vproject, slot.dataset.vcause))}</code></p>`;
  }
}
