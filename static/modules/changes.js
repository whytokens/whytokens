// @ts-check
// Changes screen: record a change you made to a habit, see every recorded change with its status,
// correct a date or note, delete a mistake. Data and the statistical rule live server side
// (fixes.py via GET /api/changes); this module only draws them. Nothing here may call a result a
// verdict unless the server says the gate is met (state is not "waiting" / "no_history").
import { esc, fmtCostBig } from './format.js';

// ?tab=changes&project=..&cause=..&note=.. fills the form. Read once at import: the app rewrites
// the query string as soon as a tab is shown.
const PREFILL = typeof location === 'undefined' ? new URLSearchParams() : new URLSearchParams(location.search);

const CHIP = {
  reduced: ['chg-ok', 'Reduced'],
  no_change: ['chg-flat', 'No material change'],
  increased: ['chg-bad', 'Increased'],
  no_history: ['chg-wait', 'Not enough history before'],
};

/** Status chip for one change from /api/changes. */
export function statusChip(c) {
  if (c.state === 'waiting') return { cls: 'chg-wait', text: `Waiting: ${c.after.sessions} of ${c.min_sessions || 10} sessions since` };
  const [cls, text] = CHIP[c.state] || ['chg-wait', 'Unknown'];
  return { cls, text };
}

/** The honest pace line for a waiting change; empty for every other state. */
export function etaLine(c) {
  if (c.state !== 'waiting') return '';
  if (c.eta_days == null) return 'No recent sessions in this project, so no estimate.';
  return `About ${c.eta_days} more day${c.eta_days === 1 ? '' : 's'} at this project's pace.`;
}

const pct = s => (s.pct * 100).toFixed(1) + '%';

function bar(label, s, max, interim) {
  const w = max > 0 ? Math.max(s.pct > 0 ? 2 : 0, (s.pct / max) * 100) : 0;
  return `<div class="chg-bar"><span class="chg-bar-label">${label}</span>` +
    `<span class="chg-track"><span class="chg-fill${interim ? ' chg-fill-interim' : ''}" style="width:${w.toFixed(1)}%"></span></span>` +
    `<span class="chg-bar-val">${pct(s)}</span>` +
    `<span class="chg-bar-n muted">${s.sessions} session${s.sessions === 1 ? '' : 's'}</span></div>`;
}

const mixList = l => (l || []).map(m => `${esc(m.label)} ${(m.pct * 100).toFixed(0)}%`).join(', ') || 'no sessions';

/** Shape mix before vs after, the server's warning as written, and the same-shape comparison (never the verdict). */
function mixHtml(m, interim) {
  let h = `<div class="shp-mix"><p class="shp-mix-line"><strong>Shape mix</strong> <span class="muted">(share of sessions)</span></p>` +
    `<p class="shp-mix-line"><span class="muted">Before:</span> ${mixList(m.before)}</p>` +
    `<p class="shp-mix-line"><span class="muted">After:</span> ${mixList(m.after)}</p>`;
  if (m.warn) h += `<p class="shp-warn" role="note">${esc(m.warning)}</p>`;
  if (!interim) for (const t of m.same_shape || []) {
    h += `<p class="muted shp-same">Same-shape comparison (not the verdict): ${esc(t.label)}: ${(t.before.pct * 100).toFixed(1)}% -> ` +
      `${(t.after.pct * 100).toFixed(1)}% of spend, ${t.before.sessions} vs ${t.after.sessions} sessions</p>`;
  }
  return h + '</div>';
}

/** Expanded body: before/after share bars, $ only when the verdict is in, the quality caveat. */
export function detailHtml(c) {
  const interim = !!c.interim;
  const max = Math.max(c.before.pct, c.after.pct);
  let h = `<div class="chg-detail${interim ? ' chg-interim' : ''}">` +
    `<p class="muted chg-share">Share of this project's spend attributed to ${esc(c.cause_label)}.</p>` +
    bar('Before', c.before, max, interim) + bar('After', c.after, max, interim);
  if (interim) h += '<p class="chg-early">Too early to call. These numbers are a preview, not a result.</p>';
  if (c.state === 'reduced') h += `<p>About <strong>${fmtCostBig(c.saving_usd)}</strong> less over the after period <span class="muted">(${esc(c.basis)})</span>.</p>`;
  h += `<p class="muted chg-caveat">${esc(c.caveat)}</p>`;
  if (c.mix) h += mixHtml(c.mix, interim);
  h += `<div class="chg-actions"><button class="filter-btn" data-act="edit" data-id="${c.fix.id}">Edit</button> ` +
    `<button class="filter-btn" data-act="delete" data-id="${c.fix.id}">Delete</button></div>` +
    `<form class="chg-edit" data-id="${c.fix.id}" hidden>` +
    `<label>Date <input type="date" name="date" value="${esc(c.fix.fix_date)}" max="${esc(c.today || '')}" required></label> ` +
    `<label class="chg-grow">Note <input type="text" name="note" value="${esc(c.fix.note)}" required></label> ` +
    `<button class="filter-btn" type="submit">Save</button> <button class="filter-btn" type="button" data-act="cancel">Cancel</button></form></div>`;
  return h;
}

/** One list row (a <details>: summary line + detail). */
export function rowHtml(c) {
  const chip = statusChip(c), eta = etaLine(c);
  return `<details class="chg-row" data-id="${c.fix.id}"><summary>` +
    `<span class="chg-date">${esc(c.fix.fix_date)}</span><span class="chg-proj">${esc(c.fix.project)}</span>` +
    `<span class="chg-cause">${esc(c.cause_label)}</span><span class="chg-note">${esc(c.fix.note)}</span>` +
    `<span class="chg-chip ${chip.cls}">${esc(chip.text)}</span>` +
    (eta ? `<span class="chg-eta muted">${esc(eta)}</span>` : '') + `</summary>${detailHtml(c)}</details>`;
}

export function emptyHtml() {
  return '<div class="chg-empty"><p><strong>How this works</strong></p><ol>' +
    '<li>Make one change to a habit, like trimming noisy tool output or splitting a long session.</li>' +
    '<li>Record it here with the date you made it.</li>' +
    '<li>Come back after about 10 sessions and see whether that cause took a smaller share of spend.</li></ol>' +
    '<p>Not sure what to change? <a href="?tab=waste">Start from the Waste tab</a>.</p></div>';
}

function formHtml(d) {
  const opt = (v, l) => `<option value="${esc(v)}">${esc(l)}</option>`;
  return `<form class="chg-form" id="chg-form"><div class="chg-form-title">Record a change</div>` +
    `<label>Project <select name="project" required>${d.projects.map(p => opt(p, p)).join('')}</select></label> ` +
    `<label>Cause <select name="cause" required>${d.causes.map(c => opt(c.id, c.label)).join('')}</select></label> ` +
    `<label>Date <input type="date" name="date" value="${esc(d.today)}" max="${esc(d.today)}" required></label> ` +
    `<label class="chg-grow">What you changed <input type="text" name="note" required placeholder="e.g. trimmed Bash output in CLAUDE.md"></label> ` +
    `<button class="filter-btn" type="submit">Record</button><p class="chg-msg" id="chg-msg" role="status"></p></form>`;
}

const token = () => document.querySelector('meta[name=whytokens-token]')?.content || '';

async function post(path, body) {
  const resp = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Whytokens-Token': token() }, body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let msg = String(resp.status);
    try { msg = (await resp.json()).error || msg; } catch (e) { /* keep status */ }
    throw new Error(msg);
  }
  return resp.json();
}

/** Delete one change after the user confirms. Returns true when it was deleted. */
export async function deleteChange(id, note) {
  if (!confirm(`Delete this recorded change${note ? ` ("${note}")` : ''}? This cannot be undone.`)) return false;
  await post('/api/fixes/delete', { id });
  return true;
}

let panelEl = null;

export async function renderChanges(panel) {
  if (!panelEl) wire(panel);
  panelEl = panel;
  let d;
  try { d = await (await fetch('/api/changes')).json(); } catch (e) { panel.textContent = 'Could not load recorded changes.'; return; }
  d.changes.forEach(c => { c.min_sessions = d.min_sessions; c.today = d.today; c.caveat = c.caveat || d.caveat; });
  const open = new Set([...panel.querySelectorAll('.chg-row[open]')].map(e => e.dataset.id));
  // A refresh (auto-rescan, theme change) must not eat what the user is typing.
  const draft = {};
  panel.querySelectorAll('#chg-form [name]').forEach(el => { draft[el.name] = el.value; });
  const edits = {};
  panel.querySelectorAll('.chg-edit:not([hidden])').forEach(f => { edits[f.dataset.id] = { date: f.date.value, note: f.note.value }; });
  panel.innerHTML = '<div class="table-card"><div class="section-title">Changes</div>' +
    '<p class="muted">A change you made to a habit, compared before and after. Only a verdict (not a preview) can claim a saving.</p>' +
    formHtml(d) + '</div><div class="table-card" id="chg-list">' +
    (d.changes.length ? d.changes.map(rowHtml).join('') : emptyHtml()) + '</div>';
  panel.querySelectorAll('.chg-row').forEach(e => { if (open.has(e.dataset.id)) e.open = true; });
  const f = panel.querySelector('#chg-form');
  if (PREFILL.get('project') && d.projects.includes(PREFILL.get('project'))) f.project.value = PREFILL.get('project');
  if (PREFILL.get('cause') && d.causes.some(c => c.id === PREFILL.get('cause'))) f.cause.value = PREFILL.get('cause');
  if (PREFILL.get('note')) f.note.value = PREFILL.get('note');
  for (const [k, v] of Object.entries(draft)) if (v && f[k] && (k !== 'project' && k !== 'cause' || [...f[k].options].some(o => o.value === v))) f[k].value = v;
  for (const [id, v] of Object.entries(edits)) {
    const ef = panel.querySelector(`.chg-edit[data-id="${id}"]`);
    if (ef) { ef.hidden = false; ef.date.value = v.date; ef.note.value = v.note; }
  }
}

function say(text) { const m = panelEl && panelEl.querySelector('#chg-msg'); if (m) m.textContent = text; }

function wire(panel) {
  panel.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target, v = Object.fromEntries(new FormData(f));
    try {
      if (f.id === 'chg-form') {
        const r = await post('/api/fixes', { project: v.project, cause: v.cause, date: v.date, note: v.note });
        PREFILL.delete('note');
        f.note.value = '';  // recorded: do not carry the draft into the next render
        await renderChanges(panel);
        say(r.duplicate ? 'That change was already recorded.' : 'Recorded.');
      } else {
        await post('/api/fixes/edit', { id: Number(f.dataset.id), date: v.date, note: v.note });
        f.hidden = true;  // saved: drop the open edit so the re-render shows the stored values
        await renderChanges(panel);
      }
    } catch (err) {
      say(err.message);
      if (f.id !== 'chg-form') alert(err.message);
    }
  });
  panel.addEventListener('click', async e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const row = b.closest('.chg-row');
    if (b.dataset.act === 'edit') row.querySelector('.chg-edit').hidden = false;
    else if (b.dataset.act === 'cancel') row.querySelector('.chg-edit').hidden = true;
    else if (b.dataset.act === 'delete') {
      try { if (await deleteChange(Number(b.dataset.id), row.querySelector('.chg-note').textContent)) await renderChanges(panel); }
      catch (err) { alert(err.message); }
    }
  });
}
