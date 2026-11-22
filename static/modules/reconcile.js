// @ts-check
// Overview "Reconcile with your bill" panel: GET/POST /api/reconcile. Every figure is computed
// server-side (reconcile.py); this only lays them out. Fetched only when the panel is opened.
import { esc, fmtCostBig } from './format.js';

const token = () => document.querySelector('meta[name=whytokens-token]')?.getAttribute('content') || '';
const money = (/** @type {number|null|undefined} */ v) => v == null ? '?' : fmtCostBig(v);
// the cent value the display rule shows (not Math.round: 1.005 shows as $1.01), so figures subtract as displayed
const cents = (/** @type {number} */ v) => Number(fmtCostBig(v).replace(/[^0-9.-]/g, '')) + 0;
const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
/** An ISO instant as the local 'YYYY-MM-DDTHH:MM:SS' a datetime-local input takes ('' for none). @param {string|null|undefined} iso */
export function localInput(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(+d)) return '';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** @param {string} url @param {RequestInit} [init] */
async function call(url, init) {
  const resp = await fetch(url, init);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || String(resp.status));
  return body;
}

/** The waterfall table for one /api/reconcile payload. @param {any} r */
export function reconcileHTML(r) {
  const head = `<div class="muted">${esc(r.period)} · ${esc(r.cut)} · ${esc(r.basis)}</div>`;
  const line = (/** @type {string} */ label, /** @type {string} */ value, cls = '') =>
    `<tr class="${cls}"><td>${esc(label)}</td><td class="num">${esc(value)}</td></tr>`;
  const billed = r.billed == null ? 'not entered'
    : money(r.billed) + (r.as_of ? ` as of ${r.as_of} (whytokens cut there too)` : '');
  // Shown at cent precision, so the displayed rows must add up at cent precision too: the gap is
  // the difference of the displayed figures and the remainder takes the rounding.
  const tie = r.gap != null && r.remainder.usd != null && r.billed != null;
  const gapD = tie ? cents(cents(r.shown) - cents(r.billed)) : r.gap;
  const remD = tie ? cents(gapD - r.rows.reduce((/** @type {number} */ a, /** @type {any} */ x) => a + cents(x.usd), 0)) : r.remainder.usd;
  const rows = [
    line(`whytokens shown (${r.tz} days)`, money(r.shown)),
    line('Billed', billed),
    r.gap == null ? '' : line('Gap (shown − billed)', money(gapD), 'rec-total'),
    ...r.rows.map((/** @type {any} */ x) => line(
      `${x.kind === 'scenario' ? 'Scenario · ' : ''}${x.label}${x.turns == null ? '' : ` (${x.turns} rows)`}`, money(x.usd))),
    line(r.remainder.label, money(remD), 'rec-total'),
  ].join('');
  const i = r.info, c = r.coverage;
  const notes = [
    `Not subtracted: ${money(i.advisor_estimates.usd)} advisor calls with estimated tokens (${i.advisor_estimates.turns} rows); ${i.raised_output_turns} turns with estimated output; ${money(i.coordinator_lower_bound.usd)} claude.ai Projects seen locally (partial).`,
    `Coverage: earliest Claude data ${c.earliest_claude || 'none'}; ${c.label}.` +
      (c.projects_first_seen_in_period.length ? ` ${c.projects_first_seen_in_period.length} project(s) first seen this month (new, or older history cleaned up before the first scan).` : ''),
  ].map(t => `<p class="muted">${esc(t)}</p>`).join('');
  return head + `<table class="rec-table"><tbody>${rows}</tbody></table>` + notes;
}

// Fixed wording (the cost-state spec): the column header and the session result both show it.
export const CC_TOOLTIP = "Claude Code's own running cost for this session, from the `cost-state` record it writes into the transcript (what `/usage` shows). It counts calls the transcript never records, such as Haiku side calls for web search and titles, so whytokens can't see them. It's an estimate at list prices, not a bill. Known faults: some Claude Code versions priced Fable 5.2 at Opus rates, and it charges for local models that whytokens prices at $0. It only covers sessions from recent Claude Code versions. \"Partial\" means a later Claude Code process restarted its counter, so the figure misses earlier turns. Neither number reflects enterprise discounts or what your invoice says.";
const CC_LABEL = /** @type {Record<string, string>} */ ({ clean: 'prices match', priced_differently: 'Claude Code prices this differently',
  unverifiable: "can't be repriced from the record", not_in_record: 'not in the record' });
const pct = (/** @type {number} */ d) => `${d >= 0 ? '+' : ''}${(d * 100).toFixed(1)}%`;

/** The Sessions-tab "Claude Code" cell for one session row. @param {any} s */
export function ccCell(s) {
  if (s.cc_coverage === 'partial') return `<td class="muted" title="${esc(CC_TOOLTIP)}">partial</td>`;
  if (s.cc_coverage !== 'full') return '<td class="muted">&mdash;</td>';
  const flag = s.cc_flag ? ' <span class="cc-flag" title="whytokens is missing or overestimating something here">&#9873;</span>' : '';
  return `<td class="cost" title="Delta: whytokens at list prices (not your local price edits) vs Claude Code">${esc(money(s.cc_cost))}${s.cc_delta_pct == null ? '' : ` <span class="muted">${esc(pct(s.cc_delta_pct))}</span>`}${flag}</td>`;
}

/** One session vs its /usage cost. @param {any} s */
export function sessionHTML(s) {
  const src = s.usage_source === 'cost-state' ? " (from Claude Code's cost-state record)" : s.usage_source === 'entered' ? ' (entered)' : '';
  const cov = s.cc_coverage === 'partial' ? " Claude Code's cost-state record is partial, so it is not compared." : '';
  const models = (s.models || []).map((/** @type {any} */ m) =>
    `<tr><td>${esc(m.model)}</td><td class="num">${esc(money(m.cc_cost))}</td><td class="num">${esc(money(m.wt_cost))}</td><td>${esc(CC_LABEL[m.label] || m.label)}</td></tr>`).join('');
  return `<p>${esc(`Session ${s.session_id}: whytokens ${money(s.whytokens)} over ${s.turns} turns (last scanned turn ${s.last_turn}), /usage ${money(s.usage)}${src}, delta ${money(s.delta)}; of which estimated summaries ${money(s.estimated)}, Haiku 5x premium ${money(s.haiku_5x)}.${cov}`)}</p>` +
    (s.cc_flag ? "<p>&#9873; whytokens and Claude Code's cost-state record differ by more than 5% with no known cause.</p>" : '') +
    (models ? `<table class="rec-table"><thead><tr><th>Model</th><th>Claude Code</th><th>whytokens</th><th>Reprice</th></tr></thead><tbody>${models}</tbody></table>` : '') +
    (s.cc_coverage !== 'none' ? `<p class="muted">${esc(CC_TOOLTIP)}</p>` : '');
}

/** Wire the panel. @param {() => string} getTZ the dashboard's display time zone ('local' | 'utc') */
export function initReconcile(getTZ) {
  const panel = /** @type {HTMLDetailsElement|null} */ (document.getElementById('reconcile'));
  if (!panel) return;
  const $ = (/** @type {string} */ id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
  const out = $('rec-out'), status = $('rec-status');
  if (!$('rec-period').value) $('rec-period').value = new Date().toISOString().slice(0, 7);  // the UTC month
  const tz = () => (getTZ() === 'utc' ? 'utc' : 'local');
  // Save stays disabled until the selected month and zone have loaded, and a reply for an older
  // selection is dropped, so one month's figure can never be saved under another.
  let gen = 0;
  let loaded = /** @type {string|null} */ (null);  // the stored reading instant, resent untouched unless the input is edited
  const show = async (/** @type {() => Promise<any>} */ get) => {
    const mine = ++gen;
    status.textContent = '';
    $('rec-save').disabled = true;
    try {
      const r = await get();
      if (mine !== gen) return;
      out.innerHTML = reconcileHTML(r);
      $('rec-billed').value = r.billed == null ? '' : String(r.billed);
      loaded = r.as_of || null;
      $('rec-asof').value = localInput(r.as_of);  // the amount and its reading time always travel together
      $('rec-save').disabled = false;
    } catch (e) { if (mine === gen) status.textContent = String(/** @type {Error} */ (e).message); }
  };
  const load = () => {
    $('rec-billed').value = '';
    $('rec-asof').value = ''; loaded = null;
    return show(() => call(`/api/reconcile?period=${encodeURIComponent($('rec-period').value)}&tz=${tz()}`));
  };
  const save = (/** @type {number|null} */ billed) => {
    const at = $('rec-asof').value;  // optional: when the bill was read (local time); empty = now
    return show(() => call('/api/reconcile', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Whytokens-Token': token() },
      body: JSON.stringify({ period: $('rec-period').value, billed, tz: tz(), as_of: !at ? null : (loaded && at === localInput(loaded) ? loaded : new Date(at).toISOString()) }),
    }));
  };
  panel.addEventListener('toggle', () => { if (panel.open) load(); });
  $('rec-period').addEventListener('change', load);
  $('rec-save').addEventListener('click', () => {
    const raw = $('rec-billed').value.trim(), v = Number(raw);
    if (!raw || !isFinite(v) || v < 0) { status.textContent = 'Enter the billed amount (0 or more), or use Clear.'; return; }
    save(v);
  });
  $('rec-clear').addEventListener('click', () => { $('rec-billed').value = ''; $('rec-asof').value = ''; loaded = null; save(null); });
  $('rec-check').addEventListener('click', async () => {
    const sid = $('rec-session').value.trim(), usage = $('rec-usage').value.trim();
    const sout = $('rec-session-out');
    if (!sid) { sout.textContent = 'Enter a session id.'; return; }
    try {
      sout.innerHTML = sessionHTML(await call(`/api/reconcile?session=${encodeURIComponent(sid)}${usage ? `&usage=${encodeURIComponent(usage)}` : ''}`));
    } catch (e) { sout.textContent = String(/** @type {Error} */ (e).message); }
  });
  return () => { if (panel.open) load(); };  // the display zone changed: refresh an open panel
}
