// @ts-check
// Dashboard application: state, filtering/aggregation, chart + table renderers,
// session drilldown, CSV export, and data loading. Pure helpers live in
// format.js / pricing.js / api.js; this module wires them to the DOM.
//
// This is an ES module, so the 59 inline on*="fn(...)" handlers in index.html
// can't see these functions (module scope isn't global). The window.* shim at
// the bottom re-exposes exactly the handlers the markup references. Splitting
// this further (charts/tables/session into their own modules) is possible but not done.

import { esc, fmt, fmtCost, fmtCostBig, fmtPct, fmtLocalDateTime } from './format.js';
import {
  DEFAULT_PRICING, PRICING, getOverrides, applyDefaultPricing,
  loadPricing, setOverride, clearOverrides,
  isBillable, isEstimated, getPricing, calcCost, rowCost,
  sessionCost, sessionCostBreakdown,
} from './pricing.js';
import { VALID_TABS, resolveTab, applyTabVisibility, addExtRailItem, toggleRail, initRail } from './nav.js';
import { store, TZ_KEY, alpha, fillTokens, initTheme, setThemePref } from './theme.js';
import { fetchData, fetchSession, fetchSessionBreakdown, fetchPricing, postRescan, fetchExtensions, fetchJson, fetchWaste, fetchStatus } from './api.js';

// Chart.js is loaded as a classic UMD script before this module (see index.html),
// so it lives on window. Aliased here so // @ts-check doesn't flag the global.
const Chart = /** @type {any} */ (window).Chart;

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];          // sorted for the projects table
let lastByProjectChart = [];     // token-sorted, drives the Top Projects bar chart
let lastByProjectBranch = [];
let lastByTool = [];
let lastByEffort = [];
let lastByMcp = [];
let lastSubagentByType = [];      // [{agent_type, tokens, dispatches}] range-filtered
let lastSubagentDispatches = [];  // full range-filtered list, tokens-desc (server-ranked)
let lastDaily = [];
let lastHourlyAgg = { hours: [], dayCount: 0 };
let lastTotals = null;
let lastCostTotals = null;
let lastHotSessions = [];
// Active screen (VALID_TABS in nav.js). Aggregation runs once per filter change; only the
// active screen is rendered. Persisted in the URL alongside range/models/session.
// Extension tabs (GET /api/extensions): name -> {label, module_url}. Their panels are
// built by loadExtensionTabs(); rendering is delegated to the extension's module.
const extTabs = new Map();
let activeTab = 'overview';
let selectedSessionDetail = null;
let sessionFilters = { project: '', branch: '', tool: '', minCost: 0, hasCacheCreation: false };
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
// Display timezone for EVERY day/hour bucket on the dashboard: the server's
// day buckets (/api/data?tz), the range bounds derived from them, the hourly
// chart and rendered timestamps. Claude's own usage page reports UTC; this
// machine's local day is the default. Read from ?tz= on first load.
let displayTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing editor (table is single-sourced in pricing.js / GET /api/pricing) ─
function renderPricingEditor() {
  const body = document.getElementById('pricing-body');
  if (!body) return;
  const overrides = getOverrides();
  body.innerHTML = Object.keys(DEFAULT_PRICING).map(model => {
    const p = PRICING[model];
    const overridden = !!overrides[model];
    const input = (field) => `<input class="pricing-input" type="number" min="0" step="0.01" value="${p[field].toFixed(2)}" data-model="${esc(model)}" data-field="${field}" onchange="onPricingChange(this)">`;
    return `<tr>
      <td><span class="model-tag">${esc(model)}</span></td>
      <td>${input('input')}</td>
      <td>${input('output')}</td>
      <td>${input('cache_read')}</td>
      <td>${input('cache_write')}</td>
      <td>${p.cache_write_1h === undefined ? '<span class="muted">n/a</span>' : input('cache_write_1h')}</td>
      <td class="pricing-source">${overridden ? 'Custom' : 'Default'}</td>
    </tr>`;
  }).join('');
}

function onPricingChange(input) {
  const model = input.dataset.model;
  const field = input.dataset.field;
  const value = parseFloat(input.value);
  if (!setOverride(model, field, value)) {
    input.value = PRICING[model]?.[field]?.toFixed(2) || '0.00';
    return;
  }
  renderPricingEditor();
  applyFilter();
  if (selectedSessionDetail && activeTab === 'trace') {
    renderSessionDetail(selectedSessionDetail);
  }
}

function resetPricing() {
  clearOverrides();
  renderPricingEditor();
  applyFilter();
  if (selectedSessionDetail && activeTab === 'trace') {
    renderSessionDetail(selectedSessionDetail);
  }
}

// ── Chart colors ───────────────────────────────────────────────────────────
// No literals here: theme.js fillTokens() refills these objects in place from the
// CSS custom properties (styles.css, light + dark), so renderers keep their names
// and a theme change is "refill, then redraw". Called at the top of every render.
const C = {};
const TOKEN_COLORS = {};   // translucent series fills
const TOKEN_HOVER = {};    // solid hover lifts
const MODEL_COLORS = [];   // categorical palette (donut / bars)
function refreshTokens() {
  fillTokens(C, TOKEN_COLORS, TOKEN_HOVER, MODEL_COLORS);
  Chart.defaults.color = C.axis;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

/** Format a Date as YYYY-MM-DD in the display timezone, so range bounds land in
 * the same calendar the server bucketed its `day` fields into (api.py _day_expr).
 * Mixing the two drifts a day at every range edge. */
function localISODate(d) {
  if (displayTZ === 'utc') return d.toISOString().slice(0, 10);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = localISODate(new Date());
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = localISODate;
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  // Inclusive of today, so "Last 7 Days" spans 7 calendar days: today-6 .. today.
  // (end is null — the filter is `day >= start` — so today-7 would span 8.)
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - (days - 1));
  return { start: iso(d), end: null };
}

function readURLTZ() {
  const q = new URLSearchParams(window.location.search).get('tz');
  return (q || store.get(TZ_KEY)) === 'utc' ? 'utc' : 'local';  // URL wins, then the Settings default
}
function syncDefaultTZButtons() {
  const def = store.get(TZ_KEY) === 'utc' ? 'utc' : 'local';
  document.querySelectorAll('[data-tzdef]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.tzdef === def)));
}
// Settings -> Default time zone: persists the default and applies it to the current view too.
function setDefaultTZ(mode) {
  store.set(TZ_KEY, mode);
  syncDefaultTZButtons();
  setDisplayTZ(mode);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setDisplayTZ(mode) {
  displayTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  updateURL();
  // Day buckets are computed server-side, so a TZ change needs a refetch —
  // re-filtering the rows already in hand would keep the old calendar.
  loadData();
  scheduleAutoRefresh();
}

// ── Model filter ───────────────────────────────────────────────────────────
function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  // Default to all models: the filter controls visibility, not billing. Local
  // (non-Anthropic) models show with N/A cost but must not be hidden by default
  // — including when billable Anthropic runs coexist in the same data.
  if (!param) return new Set(allModels);
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  if (selectedModels.size !== allModels.length) return false;
  return allModels.every(m => selectedModels.has(m));
}

// Vendor group for the model filter, from the same predicates that price a
// model: Anthropic = billable, OpenAI = plan-estimated, the rest run locally.
const MODEL_GROUPS = ['Anthropic', 'OpenAI', 'Local'];
function modelGroup(m) {
  if (isBillable(m)) return 'Anthropic';
  if (isEstimated(m)) return 'OpenAI';
  return 'Local';
}

// One chip per vendor group; clicking it opens a popover with that group's
// models. Every model keeps its checkbox in the DOM (hidden or not), so the
// selection, URL and All/None logic still work off the full model list —
// HIDDEN (no usage in the selected range) is never the same as UNSELECTED.
function buildFilterUI(allModels, keep) {
  selectedModels = keep || readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = MODEL_GROUPS.map(g => {
    const models = allModels.filter(m => modelGroup(m) === g);
    if (!models.length) return '';
    return `<details class="model-group" data-group="${g}">
      <summary class="model-cb-label">${g} <span class="mg-count"></span> &#9662;</summary>
      <div class="model-group-pop">
        <div class="mg-actions">
          <button class="filter-btn" onclick="setGroupModels('${g}', true)">All</button>
          <button class="filter-btn" onclick="setGroupModels('${g}', false)">None</button>
          <span class="mg-hint"></span>
        </div>
        <div class="mg-list">${models.map(m => {
          const checked = selectedModels.has(m);
          return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
            <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
            ${esc(m)}
          </label>`;
        }).join('')}</div>
      </div>
    </details>`;
  }).join('');
  // One popover open at a time; a click anywhere else closes it.
  container.querySelectorAll('details.model-group').forEach(d => d.addEventListener('toggle', () => {
    if (d.open) container.querySelectorAll('details.model-group[open]').forEach(o => { if (o !== d) o.open = false; });
  }));
  document.addEventListener('click', e => {
    container.querySelectorAll('details.model-group[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; });
  });
}

// Per-range pass: hide models with no turns in the range, order the rest by
// spend (then tokens), and refresh each group chip's selected/visible count.
function refreshModelFilter() {
  const { start, end } = getRangeBounds(selectedRange);
  const usage = {};
  for (const r of rawData.daily_by_model) {
    if ((start && r.day < start) || (end && r.day > end)) continue;
    const u = usage[r.model] || (usage[r.model] = { cost: 0, tokens: 0 });
    u.cost   += rowCost(r, r.day);
    u.tokens += r.input + r.output + r.cache_read + r.cache_creation;
  }
  document.querySelectorAll('#model-checkboxes details.model-group').forEach(d => {
    const list = d.querySelector('.mg-list');
    const labels = [...list.children];
    labels.sort((a, b) => {
      const ua = usage[a.dataset.model] || { cost: -1, tokens: -1 };
      const ub = usage[b.dataset.model] || { cost: -1, tokens: -1 };
      return (ub.cost - ua.cost) || (ub.tokens - ua.tokens) || a.dataset.model.localeCompare(b.dataset.model);
    }).forEach(l => list.appendChild(l));
    let visible = 0, on = 0;
    for (const l of labels) {
      const u = usage[l.dataset.model];
      l.classList.toggle('unused', !u);
      l.title = u ? `${fmtCost(u.cost)} · ${fmt(u.tokens)} tokens in range` : '';
      if (u) { visible++; if (selectedModels.has(l.dataset.model)) on++; }
    }
    const hiddenCount = labels.length - visible;
    d.classList.toggle('empty', visible === 0);
    d.querySelector('.mg-count').textContent = `${on}/${visible}`;
    d.querySelector('.mg-hint').textContent = hiddenCount ? `${hiddenCount} unused in range hidden` : '';
    const chip = d.querySelector('summary');
    chip.classList.toggle('checked', visible > 0 && on === visible);
    chip.classList.toggle('partial', on > 0 && on < visible);
  });
}

function setModelChecked(cb, on) {
  cb.checked = on;
  if (on) selectedModels.add(cb.value); else selectedModels.delete(cb.value);
  cb.closest('label').classList.toggle('checked', on);
}

function onModelToggle(cb) {
  setModelChecked(cb, cb.checked);
  updateURL();
  applyFilter();
}

// Group All/None covers the group's hidden models too, so "None" means none.
function setGroupModels(group, on) {
  document.querySelectorAll(`#model-checkboxes details[data-group="${group}"] input`).forEach(cb => setModelChecked(cb, on));
  updateURL(); applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => setModelChecked(cb, true));
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => setModelChecked(cb, false));
  updateURL(); applyFilter();
}

// ── Session filters ───────────────────────────────────────────────────────
function buildSessionFilterUI(sessions) {
  const tools = new Set();
  for (const s of sessions) {
    for (const tool of (s.tools || [])) {
      if (tool) tools.add(tool);
    }
  }
  const toolSelect = document.getElementById('session-tool-filter');
  const current = toolSelect.value;
  toolSelect.innerHTML = '<option value="">All tools</option>' + [...tools].sort((a, b) => a.localeCompare(b)).map(t =>
    `<option value="${esc(t)}">${esc(t)}</option>`
  ).join('');
  if ([...toolSelect.options].some(o => o.value === current)) toolSelect.value = current;
}

function readSessionFilters() {
  sessionFilters = {
    project: document.getElementById('session-project-filter').value.trim().toLowerCase(),
    branch: document.getElementById('session-branch-filter').value.trim().toLowerCase(),
    tool: document.getElementById('session-tool-filter').value,
    minCost: parseFloat(document.getElementById('session-min-cost-filter').value) || 0,
    hasCacheCreation: document.getElementById('session-cache-filter').checked,
  };
  renderProjectChip();
}

// The project filter is shared by chip: visible in the filter bar on every screen, removable.
function renderProjectChip() {
  const raw = /** @type {HTMLInputElement} */ (document.getElementById('session-project-filter')).value.trim();
  document.getElementById('project-chip-slot').innerHTML = raw
    ? `<span class="filter-chip">Sessions · project: ${esc(raw)}<button type="button" aria-label="Remove project filter" onclick="clearProjectChip()">&times;</button></span>` : '';
}
function clearProjectChip() {
  /** @type {HTMLInputElement} */ (document.getElementById('session-project-filter')).value = '';
  onSessionFilterChange();
}
// Overview "By project" -> Sessions pre-filtered by that project (mapping.md).
function showProjectSessions(project) {
  /** @type {HTMLInputElement} */ (document.getElementById('session-project-filter')).value = project;
  readSessionFilters();
  activeTab = 'sessions';  // applyFilter's render then paints Sessions with the new filter
  applyTabVisibility('sessions');
  updateURL();
  applyFilter();
}

function onSessionFilterChange() {
  readSessionFilters();
  applyFilter();
}

function clearSessionFilters() {
  document.getElementById('session-project-filter').value = '';
  document.getElementById('session-branch-filter').value = '';
  document.getElementById('session-tool-filter').value = '';
  document.getElementById('session-min-cost-filter').value = '';
  document.getElementById('session-cache-filter').checked = false;
  readSessionFilters();
  applyFilter();
}

function applySessionFilters(sessions) {
  return sessions.filter(s => {
    if (sessionFilters.project && !(s.project || '').toLowerCase().includes(sessionFilters.project)) return false;
    if (sessionFilters.branch && !(s.branch || '').toLowerCase().includes(sessionFilters.branch)) return false;
    if (sessionFilters.tool && !(s.tools || []).includes(sessionFilters.tool)) return false;
    if (sessionFilters.hasCacheCreation && !(s.cache_creation > 0)) return false;
    if (sessionFilters.minCost && sessionCost(s, s.last_date) < sessionFilters.minCost) return false;
    return true;
  });
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL(hash) {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (activeTab !== 'overview') params.set('tab', activeTab);
  if (activeTab === 'trace' && selectedSessionDetail) params.set('session', selectedSessionDetail.session.session_id);
  if (selectedRange !== '30d') params.set('range', selectedRange);
  // Omit tz only when it matches the Settings default, so reload restores what is shown.
  if (displayTZ !== (store.get(TZ_KEY) === 'utc' ? 'utc' : 'local')) params.set('tz', displayTZ);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search + (hash ? '#' + hash : ''));
}

function readURLSession() {
  return new URLSearchParams(window.location.search).get('session') || '';
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = sessionCost(a, a.last_date);
      bv = sessionCost(b, b.last_date);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function aggregateCostBreakdown(rows) {
  return rows.reduce((acc, r) => {
    // Per-model when a session breakdown is present (mixed-model sessions), else
    // single-model. sessionCostBreakdown handles both.
    const c = sessionCostBreakdown(r, r.last_date);
    acc.input += c.input;
    acc.output += c.output;
    acc.cache_read += c.cache_read;
    acc.cache_creation += c.cache_creation;
    acc.total += c.total;
    acc.cache_savings += c.cache_savings;
    acc.tokens += (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_creation || 0);
    acc.cache_tokens += (r.cache_read || 0) + (r.cache_creation || 0);
    return acc;
  }, { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0, tokens: 0, cache_tokens: 0 });
}

function sessionSignals(sessions) {
  return sessions.map(s => {
    const cost = sessionCost(s, s.last_date);
    const minutes = Math.max(parseFloat(s.duration_min) || 0, 1 / 60);
    const tokens = (s.input || 0) + (s.output || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
    const promptTokens = (s.input || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
    return {
      ...s,
      cost,
      cost_per_min: cost / minutes,
      tokens_per_min: tokens / minutes,
      output_per_turn: s.turns ? (s.output || 0) / s.turns : 0,
      cache_share: promptTokens ? ((s.cache_read || 0) + (s.cache_creation || 0)) / promptTokens : 0,
    };
  }).sort((a, b) => b.cost - a.cost);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
// applyFilter() is split in two:
//   aggregate()        — pure recompute of every `last*` dataset from rawData
//   renderTab(name)    — paints ONLY the named tab from those cached datasets
// applyFilter() = aggregate-once + render-the-active-tab. Tab switching renders
// from the cache with no refetch and no re-aggregation. Charts are (re)built
// when their tab is rendered, so a hidden tab's canvases never measure 0×0.
function applyFilter() {
  if (!rawData) return;
  refreshModelFilter();
  aggregate();
  renderTab(activeTab);
}

function aggregate() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    // Priced per (day, model) row and summed — same reason as modelMap below:
    // each row knows its own model AND its own day, so a tiered model stays
    // correctly priced across a range that straddles a price change.
    d.cost           += rowCost(r, r.day);
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data. Cost is priced per
  // (day, model) bucket and summed here — not recomputed from the aggregated
  // totals below — so a tiered model's cost stays correct across a range that
  // straddles a price change.
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
    m.cost           += rowCost(r, r.day);
  }

  // Filter sessions by model + date range
  const rangeFilteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );
  // Session-list filters (project/branch/tool/...) narrow only the Sessions list; Overview totals
  // and project tables stay on model + range, the same basis as the token charts.
  const filteredSessions = applySessionFilters(rangeFilteredSessions);

  // Add session counts into modelMap
  for (const s of rangeFilteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of rangeFilteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += sessionCost(s, s.last_date);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of rangeFilteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += sessionCost(s, s.last_date);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       rangeFilteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + m.cost, 0),
  };
  const costTotals = aggregateCostBreakdown(rangeFilteredSessions);

  // By tool: aggregate from server-side per-day tool data
  const toolMap = {};
  const filteredTools = (rawData.tool_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  for (const r of filteredTools) {
    if (!toolMap[r.tool]) toolMap[r.tool] = { tool: r.tool, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const t = toolMap[r.tool];
    t.input          += r.input;
    t.output         += r.output;
    t.cache_read     += r.cache_read;
    t.cache_creation += r.cache_creation;
    t.turns          += r.turns;
    t.cost           += rowCost(r, r.day);
  }
  const byTool = Object.values(toolMap).sort((a, b) => b.cost - a.cost);

  // By reasoning effort: same per-(day, model) pricing as tools above, so a
  // tiered model stays correctly priced across a range that spans a rate change.
  const effortMap = {};
  const filteredEfforts = (rawData.effort_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  for (const r of filteredEfforts) {
    if (!effortMap[r.effort]) effortMap[r.effort] = { effort: r.effort, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const e = effortMap[r.effort];
    e.input          += r.input;
    e.output         += r.output;
    e.cache_read     += r.cache_read;
    e.cache_creation += r.cache_creation;
    e.turns          += r.turns;
    e.cost           += rowCost(r, r.day);
  }
  const byEffort = Object.values(effortMap).sort((a, b) => b.cost - a.cost);

  // By MCP server: turns that consumed an MCP tool result, priced the same way.
  const mcpMap = {};
  const filteredMcp = (rawData.mcp_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  for (const r of filteredMcp) {
    if (!mcpMap[r.mcp]) mcpMap[r.mcp] = { tool: r.mcp, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost: 0 };
    const e = mcpMap[r.mcp];
    e.input          += r.input;
    e.output         += r.output;
    e.cache_read     += r.cache_read;
    e.cache_creation += r.cache_creation;
    e.turns          += r.turns;
    e.cost           += rowCost(r, r.day);
  }
  const byMcp = Object.values(mcpMap).sort((a, b) => b.cost - a.cost);

  const hotSessions = sessionSignals(filteredSessions);

  // Subagent dispatches (Task/Agent tool). Filtered by the active date range only —
  // the agents table has no model column, so the Models filter doesn't apply. The
  // server ships them tokens-desc; derive the by-type totals for the chart.
  const subagentDispatches = (rawData.subagent_dispatches || []).filter(d =>
    (!start || d.day >= start) && (!end || d.day <= end)
  );
  const subagentTypeMap = {};
  for (const d of subagentDispatches) {
    const key = d.agent_type || 'unknown';
    if (!subagentTypeMap[key]) subagentTypeMap[key] = { agent_type: key, tokens: 0, dispatches: 0 };
    subagentTypeMap[key].tokens += d.total_tokens || 0;
    subagentTypeMap[key].dispatches++;
  }
  const subagentByType = Object.values(subagentTypeMap).sort((a, b) => b.tokens - a.tokens);

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, displayTZ);

  // Cache every dataset the renderers need. Nothing is painted here — that's
  // renderTab()'s job, which runs only for the visible tab.
  lastDaily = daily;
  lastHourlyAgg = hourlyAgg;
  lastTotals = totals;
  lastCostTotals = costTotals;
  lastHotSessions = hotSessions;
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProjectChart = byProject;          // token-sorted for the bar chart
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  lastByTool = byTool;
  lastByEffort = byEffort;
  lastByMcp = byMcp;
  lastSubagentByType = subagentByType;
  lastSubagentDispatches = subagentDispatches;  // already tokens-desc from the server
}

// Paint a single tab from the cached aggregates. Charts are created here (not in
// aggregate) so they're only ever built while their tab is visible — a canvas
// in a display:none panel measures 0×0 and renders wrong. hiddenSeries persists
// across rebuilds, so legend toggles survive tab switches.
function renderTab(name) {
  refreshTokens();  // re-read CSS colours: a theme change only has to call renderTab
  if (extTabs.has(name)) { renderExtTab(name); return; }  // own data; not gated on rawData
  if (name === 'settings') { renderPricingEditor(); return; }
  if (name === 'sources') { renderSources(); return; }
  if (name === 'trace') { renderTrace(); return; }
  if (!rawData || !lastTotals) return;
  switch (name) {
    case 'overview':
      document.getElementById('daily-chart-title').textContent = 'Daily Token Usage — ' + RANGE_LABELS[selectedRange];
      document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution — ' + RANGE_LABELS[selectedRange];
      renderStats(lastTotals);
      renderDailyChart(lastDaily);
      renderHourlyChart(lastHourlyAgg);
      renderModelChart(lastByModel);
      renderProjectChart(lastByProjectChart);
      renderModelCostTable(lastByModel);
      renderEffortCostTable(lastByEffort);
      renderCostBreakdown(lastCostTotals);
      renderProjectCostTable(lastByProject);
      renderProjectBranchCostTable(lastByProjectBranch);
      renderToolSection();
      break;
    case 'sessions':
      // Pass the FULL cached arrays — each table owns its own row-reveal slicing
      // (sessionsLimit) and footer total. Slicing here would cap show-more state
      // and silently collapse an expanded table on a plain tab switch.
      renderSessionsTable(lastFilteredSessions);
      renderSessionSignalsTable(lastHotSessions.slice(0, 15));  // fixed top-15, no footer
      break;
    case 'waste':
      renderWasteTab();
      break;
  }
}

// The tool/MCP/subagent section is a collapsed <details> inside Overview. Tables are
// cheap; the subagent chart is only built while the section is open (a canvas inside a
// closed <details> measures 0x0), and again when the user opens it.
function renderToolSection() {
  renderToolCostTable(lastByTool.slice(0, 15));
  renderToolCostTable(lastByMcp, 'mcp-cost-body');
  renderSubagentDispatchTable(lastSubagentDispatches.slice(0, 25));
  if (/** @type {HTMLDetailsElement} */ (document.getElementById('tools')).open) renderSubagentChart(lastSubagentByType);
}

// ── Waste tab ───────────────────────────────────────────────────────────────
// Server-priced at API list rate (waste.py), deliberately NOT repriced with the
// browser's local overrides: one basis for every number on the tab.
const CAUSE_LABEL = {
  resident_output: 'Tool output kept in context', repeated_reads: 'Repeated file reads',
  subagent_preamble: 'Subagent preambles', idle_gap: 'Idle gaps (cache expiry)',
  uncached_input: 'Uncached input',
};
async function renderWasteTab() {
  let w;
  try { w = await fetchWaste(); } catch (e) { w = null; }
  const el = id => document.getElementById(id);
  if (!w) { el('waste-findings').textContent = 'Failed to load waste data.'; return; }
  const label = c => CAUSE_LABEL[c] || c;
  el('waste-basis').textContent = 'Priced at ' + w.basis.pricing + ' (' + w.basis.label + '). Local price overrides do not apply here.';
  el('waste-rollup-body').innerHTML = w.rollup.length ? w.rollup.map(r =>
    `<tr><td>${esc(label(r.cause))}</td><td>${esc(r.tier)}</td><td>${r.sessions}</td><td>${fmtCost(r.usd)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">No material waste found.</td></tr>';
  const m = w.measurement, srcs = m.sources;
  el('waste-measure').innerHTML = '<thead><tr><th>Cause</th>' + srcs.map(s => `<th>${esc(s)}</th>`).join('') + '</tr></thead><tbody>' +
    Object.keys(m.causes).map(c => `<tr><td>${esc(label(c))}</td>` + srcs.map(s => {
      const x = m.causes[c][s];
      return x.status === 'measured' ? '<td>measured</td>'
        : `<td class="muted" title="${esc(x.reason)}">not measurable<br><small>${esc(x.reason)}</small></td>`;
    }).join('') + '</tr>').join('') + '</tbody>';
  el('waste-findings').innerHTML = w.findings.length ? w.findings.slice(0, 100).map(f =>
    `<details><summary><button class="link-btn" data-session="${esc(f.session_id)}" onclick="openSessionDetail(this.dataset.session)">${esc(f.session_id.slice(0, 8))}&hellip;</button> ` +
    `${esc(label(f.cause))} &mdash; ${fmtCost(f.usd)} of ${fmtCost(f.session_usd)} <span class="muted">(${esc(f.tier)})</span></summary>` +
    `<p>${esc(f.detail)}</p><p><strong>${esc(f.advice)}</strong></p>` +
    '<table><thead><tr><th>Turn</th><th>Tool</th><th>Tokens</th><th>Attributed</th></tr></thead><tbody>' +
    f.evidence.map(e => `<tr><td>${esc(e.at)}</td><td>${esc(e.tool || '')}</td><td>${fmt(e.tokens)}</td><td>${fmtCost(e.usd)}</td></tr>`).join('') +
    '</tbody></table></details>').join('') : '<p class="muted">No material waste found.</p>';
}

// ── Tabs ────────────────────────────────────────────────────────────────────
// Extension tab modules export render(panelEl, ctx); ctx is the minimal helper set.
const extCtx = { fetchJson, esc, fmt, fmtCost, fmtPct, rowCost, downloadCSV,
  openSessionDetail: id => openSessionDetail(id) };

async function renderExtTab(name) {
  const panel = document.getElementById('panel-' + name);
  try {
    const mod = await import(extTabs.get(name).module_url);
    await mod.render(panel, extCtx);
  } catch (e) {
    console.error('Extension tab failed: ' + name, e);
    panel.textContent = 'Failed to load the ' + name + ' tab.';
  }
}

// Append each extension's rail item + panel (same ids/classes as the built-in screens).
// A failed /api/extensions leaves core exactly as it was.
async function loadExtensionTabs() {
  let tabs = [];
  try { tabs = (await fetchExtensions()).tabs || []; } catch (e) { /* no extensions */ }
  for (const t of tabs) {
    if (VALID_TABS.includes(t.name) || !/^[\w-]+$/.test(t.name)) continue;
    VALID_TABS.push(t.name);
    extTabs.set(t.name, t);
    addExtRailItem(t);
  }
}

// ?tab= (legacy ids redirected per nav.js) + the URL hash -> { tab, hash }.
function readURLTab() {
  const q = new URLSearchParams(window.location.search);
  const { tab, hash } = resolveTab(q.get('tab'), window.location.hash);
  // A bare ?session=ID (old deep link) means the Trace screen.
  return { tab: !q.get('tab') && q.get('session') ? 'trace' : tab, hash };
}

function setTab(name, hash) {
  if (!VALID_TABS.includes(name)) name = 'overview';
  activeTab = name;
  applyTabVisibility(name);
  updateURL(hash);
  renderTab(name);
  gotoSection(hash);
}

// #projects / #tools / #pricing: open the (collapsed) section, then scroll to it.
function gotoSection(hash) {
  const el = hash ? document.getElementById(hash) : null;
  if (!el) return;
  if (el.tagName === 'DETAILS' && !el.open) { el.open = true; renderTab(activeTab); }
  el.scrollIntoView();
}

function wireTabs() {
  // Delegated so extension rail items (added before this runs, but still) need no wiring.
  document.getElementById('tab-bar').addEventListener('click', e => {
    const btn = e.target.closest('.tab-btn[data-tab]');
    if (btn) setTab(btn.dataset.tab);
  });
  const tools = document.getElementById('tools');
  tools.addEventListener('toggle', () => { if (tools.open && activeTab === 'overview') { refreshTokens(); renderSubagentChart(lastSubagentByType); } });
  // Sortable headers are <th onclick>: make them focusable and Enter/Space-activatable.
  document.querySelectorAll('th.sortable').forEach(th => th.tabIndex = 0);
  document.addEventListener('keydown', e => {
    const th = e.target.closest && e.target.closest('th.sortable');
    if (th && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); th.click(); }
  });
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Sep 2026', color: C.green },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function renderCostBreakdown(c) {
  const cacheShare = c.tokens ? c.cache_tokens / c.tokens : 0;
  document.getElementById('cost-breakdown-body').innerHTML = `<tr>
    <td class="cost">${fmtCost(c.input)}</td>
    <td class="cost">${fmtCost(c.output)}</td>
    <td class="cost">${fmtCost(c.cache_read)}</td>
    <td class="cost">${fmtCost(c.cache_creation)}</td>
    <td class="cost">${fmtCostBig(c.total)}</td>
    <td class="cost">${fmtCostBig(c.cache_savings)}</td>
    <td class="num">${fmtPct(cacheShare)}</td>
  </tr>`;
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(displayTZ)
    : 'No data · ' + tzDisplayName(displayTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? alpha(C.red, 0.9) : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? C.red : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: alpha(C.accent, 0.15),
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(displayTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.daily.has('Input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         hidden: hiddenSeries.daily.has('Output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     hidden: hiddenSeries.daily.has('Cache Read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', hidden: hiddenSeries.daily.has('Cache Creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' },
        // Cost overlay on its own axis (purple — the four token colors are taken).
        // Ported from upstream claude-usage.
        { type: 'line', label: 'Est. Cost', hidden: hiddenSeries.daily.has('Est. Cost'), data: daily.map(d => d.cost), borderColor: C.purple, backgroundColor: 'transparent', pointBackgroundColor: C.purple, pointRadius: 3, tension: 0.3, yAxisID: 'y2' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: item => item.dataset.label === 'Est. Cost'
            ? ` Est. Cost: ${fmtCostBig(item.raw)}`
            : ` ${item.dataset.label}: ${fmt(item.raw)}`
        } },
      },
      scales: {
        x: { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green, callback: v => fmt(v) }, grid: { color: C.border }, title: { display: true, text: 'Cache', color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue, callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: C.blue } },
        y2: { position: 'right', ticks: { color: C.purple, callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false }, title: { display: true, text: 'Est. Cost', color: C.purple }, offset: true },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '…' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  hidden: hiddenSeries.project.has('Input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= TABLE_STEPS[0]) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">Show less ▴</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">Download CSV to see all (' + total + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to 10
// and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }

// Cost <td> for a single-model row: real cost for Anthropic, a labeled "~$x"
// API-equivalent estimate for plan-included Codex/GPT, n/a for everything else.
// Aggregate tables (project/branch/tool) mix models, so they can't label per row —
// they sum whatever calcCost prices (now including the Codex estimate).
function costCellHTML(model, cost) {
  if (isBillable(model)) return `<td class="cost">${fmtCost(cost)}</td>`;
  if (isEstimated(model)) {
    return `<td class="cost cost-est" title="API-equivalent estimate — Codex/GPT is plan-included; priced at OpenAI API rates">${fmtCost(cost)}</td>`;
  }
  return `<td class="cost-na">n/a</td>`;
}

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, sessionsLimit);
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cell = costCellHTML(s.model, sessionCost(s, s.last_date));
    const titleCell = s.topic
      ? `<td class="session-topic">${esc(s.topic)}</td>`
      : `<td class="session-topic muted">Untitled</td>`;
    return `<tr class="session-row" data-session-row="${esc(s.full_session_id)}" data-session-day="${esc(s.last_date || '')}">
      <td class="muted" style="font-family:monospace;white-space:nowrap"><button class="row-toggle" type="button" aria-expanded="false" data-breakdown="${esc(s.full_session_id)}" title="Who spent this session: main thread vs each subagent">&#9656;</button><button class="link-btn" data-session="${esc(s.full_session_id)}" onclick="openSessionDetail(this.dataset.session)">${esc(s.session_id)}&hellip;</button></td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted">${esc(fmtLocalDateTime(s.last_ts, displayTZ) || s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="num">${fmt(s.cache_read)}</td>
      <td class="num">${fmt(s.cache_creation)}</td>
      ${cell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

// ── Session breakdown sub-rows (who spent the session) ────────────────────
// Lazy: nothing is fetched until a caret is clicked, then cached per session for
// the life of the page. Sub-rows are injected after their parent <tr> rather than
// pre-rendered, and a table re-render drops them — same purely-view-state contract
// as the project table's branch rows.
const sessionBreakdownCache = new Map();

/** Roll the per-(bucket, model) API rows up to one row per bucket, priced per model. */
function summarizeBreakdown(rows, day) {
  const byLabel = new Map();
  for (const r of rows) {
    const cur = byLabel.get(r.label) || {
      label: r.label, kind: r.kind, models: new Map(), slices: [],
      turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0,
    };
    cur.models.set(r.model, (cur.models.get(r.model) || 0) + r.turns);
    cur.slices.push(r);
    cur.turns += r.turns;
    cur.input += r.input;
    cur.output += r.output;
    cur.cache_read += r.cache_read;
    cur.cache_creation += r.cache_creation;
    // Price each (bucket, model) slice with its own model — a single agent type
    // can span models, and summing tokens before pricing would misprice it.
    // calcCost takes TOTAL cache creation plus the 1h portion of it (it bills the
    // remainder at the 5m rate); passing the 5m slice as the total zeroes the whole
    // cache-creation charge, which is most of a Claude Code session's write cost.
    const sliceCost = rowCost(r, day);
    cur.slices[cur.slices.length - 1].cost = sliceCost;
    cur.cost += sliceCost;
    byLabel.set(r.label, cur);
  }
  return [...byLabel.values()].sort((a, b) => b.cost - a.cost);
}

function breakdownRowsHTML(sessionId, rows, day) {
  const buckets = summarizeBreakdown(rows, day);
  if (!buckets.length) {
    return `<tr class="breakdown-subrow" data-parent-session="${esc(sessionId)}">
      <td class="branch-cell muted" colspan="12">No per-actor detail recorded for this session.</td>
    </tr>`;
  }
  const total = buckets.reduce((sum, b) => sum + b.cost, 0);
  return buckets.map(b => {
    const share = total > 0 ? Math.round((b.cost / total) * 100) : 0;
    // A bucket spans models when its agents ran on different ones (several
    // dispatches of the same type, or one that switched mid-run). Break those out
    // into their own rows rather than folding them into a bare count.
    const models = [...b.models];
    const modelCell = models.length === 1
      ? `<span class="model-tag">${esc(models[0][0])}</span>`
      : `<span class="muted">${models.length} models</span>`;
    return `<tr class="breakdown-subrow" data-parent-session="${esc(sessionId)}">
      <td class="branch-cell">&#8627; ${esc(b.label)}</td>
      <td class="muted">${b.kind === 'main' ? '' : esc(b.kind)}</td>
      <td class="muted">${share}% of session</td>
      <td></td>
      <td></td>
      <td>${modelCell}</td>
      <td class="num">${b.turns}</td>
      <td class="num">${fmt(b.input)}</td>
      <td class="num">${fmt(b.output)}</td>
      <td class="num">${fmt(b.cache_read)}</td>
      <td class="num">${fmt(b.cache_creation)}</td>
      <td class="cost">${fmtCost(b.cost)}</td>
    </tr>` + (models.length > 1 ? modelRowsHTML(sessionId, b) : '');
  }).join('');
}

/** Per-model rows for one multi-model bucket, costliest first. */
function modelRowsHTML(sessionId, bucket) {
  return [...bucket.slices].sort((a, c) => c.cost - a.cost).map(sl => `
    <tr class="breakdown-subrow model-subrow" data-parent-session="${esc(sessionId)}">
      <td class="branch-cell"></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td><span class="model-tag">${esc(sl.model)}</span></td>
      <td class="num">${sl.turns}</td>
      <td class="num">${fmt(sl.input)}</td>
      <td class="num">${fmt(sl.output)}</td>
      <td class="num">${fmt(sl.cache_read)}</td>
      <td class="num">${fmt(sl.cache_creation)}</td>
      <td class="cost">${fmtCost(sl.cost)}</td>
    </tr>`).join('');
}

// One delegated listener for the sessions table, mirroring wireProjectExpand.
function wireSessionExpand() {
  const body = document.getElementById('sessions-body');
  if (!body) return;
  body.addEventListener('click', async e => {
    const btn = /** @type {HTMLElement} */ (e.target).closest('.row-toggle');
    if (!btn) return;
    const sessionId = /** @type {HTMLElement} */ (btn).dataset.breakdown;
    if (!sessionId) return;
    const row = btn.closest('tr');
    const willExpand = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(willExpand));
    btn.innerHTML = willExpand ? '&#9662;' : '&#9656;';
    body.querySelectorAll(`tr.breakdown-subrow[data-parent-session="${CSS.escape(sessionId)}"]`)
      .forEach(r => r.remove());
    if (!willExpand || !row) return;

    let payload = sessionBreakdownCache.get(sessionId);
    if (!payload) {
      row.insertAdjacentHTML('afterend',
        `<tr class="breakdown-subrow" data-parent-session="${esc(sessionId)}">
           <td class="branch-cell muted" colspan="12">Loading&hellip;</td></tr>`);
      try {
        payload = await fetchSessionBreakdown(sessionId);
        sessionBreakdownCache.set(sessionId, payload);
      } catch (err) {
        payload = { error: String(err) };
      }
      body.querySelectorAll(`tr.breakdown-subrow[data-parent-session="${CSS.escape(sessionId)}"]`)
        .forEach(r => r.remove());
      // The caret may have been collapsed (or the table re-rendered) while the
      // request was in flight; don't paint stale rows over a closed row.
      if (btn.getAttribute('aria-expanded') !== 'true' || !row.isConnected) return;
    }
    const day = /** @type {HTMLElement} */ (row).dataset.sessionDay || undefined;
    const html = payload.error
      ? `<tr class="breakdown-subrow" data-parent-session="${esc(sessionId)}">
           <td class="branch-cell muted" colspan="12">${esc(payload.error)}</td></tr>`
      : breakdownRowsHTML(sessionId, payload.rows || [], day);
    row.insertAdjacentHTML('afterend', html);
  });
}

function renderToolCostTable(rows, bodyId = 'tool-cost-body') {
  const body = document.getElementById(bodyId);
  if (!body) return;
  body.innerHTML = rows.map(t => {
    const costCell = t.cost ? `<td class="cost">${fmtCost(t.cost)}</td>` : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td>${esc(t.tool)}</td>
      <td class="num">${fmt(t.turns)}</td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// Cost by reasoning effort (Overview). 'unknown' covers Claude turns from before
// 2.1.215 (which never wrote the field) and Codex sessions that never announced one.
function renderEffortCostTable(rows) {
  const body = document.getElementById('effort-cost-body');
  if (!body) return;
  body.innerHTML = rows.map(e => {
    const costCell = e.cost ? `<td class="cost">${fmtCost(e.cost)}</td>` : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td>${esc(e.effort)}</td>
      <td class="num">${fmt(e.turns)}</td>
      <td class="num">${fmt(e.input)}</td>
      <td class="num">${fmt(e.output)}</td>
      <td class="num">${fmt(e.cache_read)}</td>
      <td class="num">${fmt(e.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Subagent views (Tools tab) ──────────────────────────────────────────────
// Horizontal bar of tokens by agent type. Reuses the warm categorical palette.
function renderSubagentChart(byType) {
  const canvas = document.getElementById('chart-subagent');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [{
        label: 'Tokens',
        data: byType.map(t => t.tokens),
        backgroundColor: MODEL_COLORS,
        hoverBackgroundColor: MODEL_COLORS,
      }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => {
          const t = byType[ctx.dataIndex];
          return ` ${fmt(ctx.parsed.x)} tokens · ${t.dispatches} dispatch${t.dispatches === 1 ? '' : 'es'}`;
        } } },
      },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      },
    },
  });
}

// Ranked dispatch table (server sends the full set tokens-desc; we show the top slice).
function renderSubagentDispatchTable(rows) {
  const body = document.getElementById('subagent-dispatch-body');
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No subagent dispatches in range.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(d => {
    const sid = d.session_id || '';
    const sessionCell = sid
      ? `<td class="muted" style="font-family:monospace"><button class="link-btn" data-session="${esc(sid)}" onclick="openSessionDetail(this.dataset.session)">${esc(sid.slice(0, 8))}&hellip;</button></td>`
      : `<td class="muted">—</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(d.agent_type)}</span></td>
      ${sessionCell}
      <td>${esc(d.project)}</td>
      <td class="num">${fmt(d.total_tokens)}</td>
      <td class="num">${esc(fmtWall((d.total_duration_ms || 0) / 1000))}</td>
      <td class="num">${fmt(d.tool_use_count)}</td>
    </tr>`;
  }).join('');
}

function renderSessionSignalsTable(rows) {
  document.getElementById('session-signals-body').innerHTML = rows.map(s => {
    return `<tr>
      <td class="muted" style="font-family:monospace"><button class="link-btn" data-session="${esc(s.full_session_id)}" onclick="openSessionDetail(this.dataset.session)">${esc(s.session_id)}&hellip;</button></td>
      <td>${esc(s.project)}</td>
      <td class="cost">${fmtCost(s.cost)}</td>
      <td class="cost">${fmtCost(s.cost_per_min)}</td>
      <td class="num">${fmt(s.tokens_per_min)}</td>
      <td class="num">${fmt(s.output_per_turn)}</td>
      <td class="num">${fmtPct(s.cache_share)}</td>
    </tr>`;
  }).join('');
}

// ── Tool call detail (compact summary + slide-in panel) ─────────────────────
function toolTruncate(s, n) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function toolBaseName(p) {
  if (!p) return '';
  return String(p).replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop();
}
// Compact one-line label for a single tool call: "Bash: git status", "Skill: humanizer", …
function toolCallSummary(tc) {
  const name = tc.name || 'tool';
  const inp = tc.input || {};
  switch (name) {
    case 'Bash':
    case 'exec':        // Codex's shell tool; sources/codex.py stores its text as input.command
    case 'PowerShell':  return name + ': ' + toolTruncate(inp.description || inp.command, 64);
    case 'Skill':       return 'Skill: ' + (inp.skill || '?') + (inp.args ? ' ' + toolTruncate(inp.args, 30) : '');
    case 'Read':        return 'Read: ' + toolBaseName(inp.file_path);
    case 'Write':       return 'Write: ' + toolBaseName(inp.file_path);
    case 'Edit':        return 'Edit: ' + toolBaseName(inp.file_path);
    case 'NotebookEdit':return 'NotebookEdit: ' + toolBaseName(inp.notebook_path || inp.file_path);
    case 'Grep':        return 'Grep: ' + toolTruncate(inp.pattern, 44);
    case 'Glob':        return 'Glob: ' + toolTruncate(inp.pattern, 44);
    case 'Agent':       return 'Agent: ' + (inp.subagent_type || toolTruncate(inp.description, 30));
    case 'WebSearch':   return 'WebSearch: ' + toolTruncate(inp.query, 44);
    case 'WebFetch':    return 'WebFetch: ' + toolTruncate(inp.url, 44);
    case 'Task':        return 'Task: ' + toolTruncate(inp.subject || inp.description, 40);
    case 'ToolSearch':  return 'ToolSearch: ' + toolTruncate(inp.query, 40);
    default:
      if (name.endsWith('.exec') && inp.command) return name + ': ' + toolTruncate(inp.command, 64);
      if (name.indexOf('mcp__') === 0) {
        const parts = name.split('__');
        return 'mcp/' + parts[parts.length - 1];
      }
      return name;
  }
}
// Table cell: compact summary of all tool calls in the turn + a "details" link.
function toolCellHTML(t, i) {
  const calls = t.tool_calls || [];
  if (!calls.length) return esc(t.tool || '<none>');
  let label = toolCallSummary(calls[0]);
  if (calls.length > 1) label += ' +' + (calls.length - 1);
  return esc(label) +
    ` <button class="link-btn tool-detail-link" onclick="openToolPanel(${i})" title="Show full tool inputs">details</button>`;
}
function openToolPanel(i) {
  const t = (selectedSessionDetail && selectedSessionDetail.turns) ? selectedSessionDetail.turns[i] : null;
  if (!t) return;
  const calls = t.tool_calls || [];
  document.getElementById('tool-panel-title').textContent =
    calls.length + (calls.length === 1 ? ' tool call' : ' tool calls');
  document.getElementById('tool-panel-sub').textContent = (t.timestamp || '') + (t.model ? ' · ' + t.model : '');
  document.getElementById('tool-panel-body').innerHTML = calls.map(tc => {
    let body = '';
    const inp = tc.input || {};
    if ((tc.name === 'Bash' || tc.name === 'PowerShell' || tc.name === 'exec' || (tc.name || '').endsWith('.exec')) && inp.command) {
      if (inp.description) body += `<div class="field">Description</div><pre>${esc(inp.description)}</pre>`;
      body += `<div class="field">Command</div><pre>${esc(inp.command)}</pre>`;
    } else {
      body = `<pre>${esc(JSON.stringify(inp, null, 2))}</pre>`;
    }
    return `<div class="tool-call-block"><h4>${esc(tc.name || 'tool')}</h4>${body}</div>`;
  }).join('') || '<p class="muted">No tool inputs recorded for this turn.</p>';
  document.getElementById('tool-detail-overlay').classList.remove('hidden');
  document.getElementById('tool-detail-panel').classList.remove('hidden');
}
function closeToolPanel() {
  document.getElementById('tool-detail-overlay').classList.add('hidden');
  document.getElementById('tool-detail-panel').classList.add('hidden');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeToolPanel(); });

function renderSessionDetail(d) {
  selectedSessionDetail = d;
  const s = d.session;
  refreshTokens();
  // Price from the per-turn models (d.turns), not the single session label — a
  // session that spawned a subagent on another model is otherwise mispriced.
  const c = sessionCostBreakdown({ model: s.model, by_model: d.turns }, (s.last || '').slice(0, 10));
  const tokens = (s.input || 0) + (s.output || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
  const promptTokens = (s.input || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
  document.getElementById('trace-empty').classList.add('hidden');
  document.getElementById('session-detail-view').classList.remove('hidden');
  document.getElementById('session-detail-title').textContent = 'Session Drilldown — ' + s.display_id + '…';
  document.getElementById('session-detail-subtitle').textContent = s.session_id + ' · ' + s.project;
  document.getElementById('session-detail-stats').innerHTML = [
    ['Project', s.project],
    ['Model', s.model],
    ['Duration', s.duration_min + 'm'],
    ['Turns', fmt(s.turns)],
    ['Tokens', fmt(tokens)],
    ['Cost', fmtCost(c.total)],
    ['Cache Share', fmtPct(promptTokens ? ((s.cache_read || 0) + (s.cache_creation || 0)) / promptTokens : 0)],
    ['Cache Savings', fmtCost(c.cache_savings)],
  ].map(([label, value]) => `<div class="detail-stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
  document.getElementById('session-detail-body').innerHTML = d.turns.map((t, i) => {
    const cost = rowCost(t, t.timestamp.slice(0, 10));
    return `<tr>
      <td class="muted">${esc(t.ts.replace('T', ' ').replace('Z', ''))}</td>
      <td>${esc(t.actor)}${t.estimated ? ' <span class="muted" title="token counts estimated">(est.)</span>' : ''}</td>
      <td class="muted">${t.gap_s == null ? '' : fmtGap(t.gap_s)}</td>
      <td class="num">${fmt(t.context)}</td>
      <td>${toolCellHTML(t, i)}</td>
      <td><span class="model-tag">${esc(t.model)}</span></td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num">${fmt(t.cache_read)}</td>
      <td class="num">${fmt(t.cache_creation)}</td>
      <td class="num">${fmt(t.cache_creation_5m || 0)}</td>
      <td class="num">${fmt(t.cache_creation_1h || 0)}</td>
      <td class="muted">${t.duration_ms ? (t.duration_ms / 1000).toFixed(1) + 's' : ''}</td>
      <td class="muted">${esc(t.stop_reason || '')}</td>
      <td class="muted">${esc(t.service_tier || '')}</td>
      ${costCellHTML(t.model, cost)}
    </tr>`;
  }).join('');
  document.getElementById('session-actors').textContent = d.actors.map(a =>
    `${a.actor}: ${fmt(a.turns)} turns` + (a.estimated_turns ? ` (${a.estimated_turns} estimated)` : '') +
    `, peak context ${fmt(a.peak_context)}`).join(' · ');
  renderSessionToolBreakdown(d.turns);
  renderSessionTimeline(d.turns);
}

// Trace screen: the loaded session if any, otherwise the empty state.
function renderTrace() {
  if (selectedSessionDetail) renderSessionDetail(selectedSessionDetail);
}

function fmtGap(sec) {
  return sec < 60 ? sec.toFixed(1) + 's' : sec < 3600 ? (sec / 60).toFixed(1) + 'm' : (sec / 3600).toFixed(1) + 'h';
}

// Pre-existing gap: renderSubagentDispatchTable called fmtWall, which was never defined (it threw
// whenever a dispatch existed). That table now renders on Overview, so define it here.
function fmtWall(sec) { return fmtGap(sec); }

function renderSessionToolBreakdown(turns) {
  const byTool = {};
  for (const t of turns) {
    const tool = t.tool || '<none>';
    if (!byTool[tool]) byTool[tool] = { tool, turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const row = byTool[tool];
    row.turns++;
    row.input += t.input || 0;
    row.output += t.output || 0;
    row.cache_read += t.cache_read || 0;
    row.cache_creation += t.cache_creation || 0;
    row.cost += rowCost(t, t.timestamp.slice(0, 10));
  }
  const rows = Object.values(byTool).sort((a, b) => b.cost - a.cost);
  const totalCost = rows.reduce((sum, r) => sum + r.cost, 0);
  document.getElementById('session-tool-breakdown-body').innerHTML = rows.map(r => {
    return `<tr>
      <td>${esc(r.tool)}</td>
      <td class="num">${fmt(r.turns)}</td>
      <td class="num">${fmt(r.input)}</td>
      <td class="num">${fmt(r.output)}</td>
      <td class="num">${fmt(r.cache_read)}</td>
      <td class="num">${fmt(r.cache_creation)}</td>
      <td class="cost">${fmtCost(r.cost)}</td>
      <td class="num">${fmtPct(totalCost ? r.cost / totalCost : 0)}</td>
    </tr>`;
  }).join('');
}

function renderSessionTimeline(turns) {
  const ctx = document.getElementById('chart-session-timeline').getContext('2d');
  if (charts.sessionTimeline) charts.sessionTimeline.destroy();
  const labels = turns.map((t, i) => (i + 1) + ' · ' + t.actor + ' · ' + (t.tool || '<none>'));
  charts.sessionTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',          data: turns.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens', yAxisID: 'y' },
        { label: 'Output',         data: turns.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens', yAxisID: 'y' },
        { label: 'Cache Read',     data: turns.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens', yAxisID: 'y' },
        { label: 'Cache Creation', data: turns.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens', yAxisID: 'y' },
        {
          label: 'Cost',
          type: 'line',
          data: turns.map(t => rowCost(t, t.timestamp.slice(0, 10))),
          borderColor: C.green,
          backgroundColor: alpha(C.green, 0.18),
          pointRadius: 3,
          tension: 0.25,
          yAxisID: 'y1',
        },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: C.axis, boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: items => {
              const t = turns[items[0].dataIndex];
              return (t.ts || '') + ' · ' + t.actor + ' · ' + (t.tool || '<none>') + (t.estimated ? ' (est.)' : '');
            },
            label: item => item.dataset.label === 'Cost'
              ? ' Cost: ' + fmtCost(item.parsed.y)
              : ' ' + item.dataset.label + ': ' + fmt(item.parsed.y),
          }
        }
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: C.border } },
        y: { stacked: true, beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.green, callback: v => '$' + Number(v).toFixed(2) }, grid: { drawOnChartArea: false } },
      }
    }
  });
}

function closeSessionDetail() { setTab('sessions'); }

async function openSessionDetail(sessionId) {
  try {
    const d = await fetchSession(sessionId);
    if (d.error) throw new Error(d.error);
    selectedSessionDetail = d;
    setTab('trace');  // panel visible first: the timeline canvas must not be built at 0x0
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch(e) {
    console.error(e);
  }
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = a.cost;
      bv = b.cost;
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, modelLimit);
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
    const cell = costCellHTML(m.model, m.cost);
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${cell}
    </tr>`;
  }).join('');
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, projectLimit);
  // Group branch rows by project so a project row can expand to its branches.
  const branchesByProject = {};
  for (const pb of lastByProjectBranch) {
    (branchesByProject[pb.project] = branchesByProject[pb.project] || []).push(pb);
  }
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    const branches = branchesByProject[p.project] || [];
    // Only worth expanding when there's more than one branch — a lone branch
    // just repeats the project totals.
    const expandable = branches.length > 1;
    const firstCell = expandable
      ? `<button class="row-toggle" type="button" aria-expanded="false" aria-label="Show branches of ${esc(p.project)}" data-project="${esc(p.project)}">▸</button>${esc(p.project)}`
      : `<span class="row-toggle-spacer"></span>${esc(p.project)}`;
    const sessionsLink = ` <button class="link-btn tool-detail-link" type="button" data-sessions-of="${esc(p.project)}" title="Show this project's sessions">sessions &rsaquo;</button>`;
    const mainRow = `<tr class="project-row">
      <td>${firstCell}${sessionsLink}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
    const branchRows = expandable ? branches.map(b => `<tr class="branch-subrow" data-parent="${esc(p.project)}" hidden>
      <td class="branch-cell">↳ ${esc(b.branch || '—')}</td>
      <td class="num">${b.sessions}</td>
      <td class="num">${fmt(b.turns)}</td>
      <td class="num">${fmt(b.input)}</td>
      <td class="num">${fmt(b.output)}</td>
      <td class="cost">${fmtCost(b.cost)}</td>
    </tr>`).join('') : '';
    return mainRow + branchRows;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// One delegated listener for the project table: clicking a ▸ caret toggles that
// project's branch sub-rows. Re-rendering the table resets all carets to ▸ and
// re-hides sub-rows, which is fine (the toggle is purely a view state).
function wireProjectExpand() {
  const body = document.getElementById('project-cost-body');
  if (!body) return;
  body.addEventListener('click', e => {
    const go = e.target.closest('[data-sessions-of]');
    if (go) { showProjectSessions(go.dataset.sessionsOf); return; }
    const btn = e.target.closest('.row-toggle');
    if (!btn) return;
    const project = btn.dataset.project;
    const willExpand = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(willExpand));
    btn.textContent = willExpand ? '▾' : '▸';
    body.querySelectorAll('.branch-subrow').forEach(r => {
      if (r.dataset.parent === project) r.hidden = !willExpand;
    });
  });
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' ▼' : ' ▲';
}

function sortProjectBranch(rows) {
  return [...rows].sort((a, b) => {
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    if (pa < pb) return -1;
    if (pa > pb) return 1;
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, branchLimit);
  document.getElementById('project-branch-cost-body').innerHTML = shown.map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '—')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportModelCSV() {
  const header = ['Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    return [m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, m.cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = sessionCost(s, s.last_date);
    return [s.session_id, s.project, fmtLocalDateTime(s.last_ts, displayTZ) || s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportSessionTurnsCSV() {
  if (!selectedSessionDetail) return;
  const header = ['Time', 'Tool', 'Model', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Cache 5m', 'Cache 1h', 'Duration (ms)', 'Stop Reason', 'Service Tier', 'Inference Geo', 'Sidechain', 'Compact Summary', 'Est. Cost'];
  const rows = selectedSessionDetail.turns.map(t => {
    const cost = rowCost(t, t.timestamp.slice(0, 10));
    return [t.timestamp, t.tool, t.model, t.input, t.output, t.cache_read, t.cache_creation, t.cache_creation_5m || 0, t.cache_creation_1h || 0, t.duration_ms || 0, t.stop_reason || '', t.service_tier || '', t.inference_geo || '', t.is_sidechain ? 1 : 0, t.is_compact_summary ? 1 : 0, cost.toFixed(4)];
  });
  downloadCSV('session_' + selectedSessionDetail.session.display_id + '_turns', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  if (btn.disabled) return; // a scan (manual or auto) is already running
  btn.disabled = true;
  btn.textContent = '↻ Scanning...';
  try {
    const d = await postRescan();
    btn.textContent = d.error ? '↻ Rescan (error)'
      : d.running ? '↻ Scan already running'
      : '↻ Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
    refreshStatus();
  } catch(e) {
    btn.textContent = '↻ Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '↻ Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
let lastStatus = null;
let startHash = '';
// Sources screen: what /api/status reports (roots, scan state, hint). Per-source file
// counts and last-scan times are not in that payload, so they are not shown.
function renderSources() {
  const st = lastStatus;
  if (!st) return;
  const sc = st.scan || {};
  document.getElementById('sources-scan').textContent =
    (sc.state === 'running' ? 'Scanning ' + (sc.source || '') + ' (' + sc.done + ' of ' + sc.total + ' files).'
      : sc.state === 'error' ? 'Last scan failed: ' + sc.error
      : 'Idle.') + ' ' + st.sessions.toLocaleString() + ' sessions in the database.' +
    (rawData ? ' Updated: ' + rawData.generated_at : '');
  document.getElementById('sources-body').innerHTML = st.roots.map(r =>
    `<tr><td>${esc(r.source)}</td><td class="muted">${esc(r.path)}</td><td>${r.exists ? 'found' : '<span class="muted">not found</span>'}</td></tr>`).join('')
    || '<tr><td colspan="3" class="muted">No transcript folders configured.</td></tr>';
  document.getElementById('sources-note').textContent = st.hint || '';
}

/** First-ten-minutes banner: scan progress, file errors, roots, and the empty-state hint. */
async function refreshStatus() {
  const el = document.getElementById('status-banner');
  let st;
  try { st = await fetchStatus(); } catch (e) { return; }
  const sc = st.scan || {};
  const lines = [];
  if (sc.state === 'running') {
    lines.push('Scanning ' + esc(sc.source || '...') + ' (' + sc.done + ' of ' + sc.total + ' files). Data appears as it lands.');
  }
  if (sc.state === 'error') lines.push('<span class="bad">Scan failed: ' + esc(sc.error) + '</span>');
  if (st.hint) lines.push(esc(st.hint));
  const errs = (sc.file_errors || []).map(e => '<li class="bad">' + esc(e.path) + ': ' + esc(e.error) + '</li>').join('');
  const showRoots = st.empty || sc.state === 'running' || errs;
  const roots = showRoots ? '<ul>' + st.roots.map(r => '<li>' + esc(r.source) + ': ' + esc(r.path) +
    (r.exists ? '' : ' (not found)') + '</li>').join('') + errs + '</ul>' : '';
  el.innerHTML = lines.join('<br>') + roots;
  el.hidden = !el.innerHTML;
  lastStatus = st;
  if (activeTab === 'sources') renderSources();
  if (sc.state === 'running') {
    setTimeout(async () => { await refreshStatus(); await loadData(); }, 1000);
  }
}

async function loadData() {
  try {
    const d = await fetchData(displayTZ);
    if (d.error) {
      document.body.innerHTML = '<div class="err-page">' + esc(d.error) + '</div>';
      return;
    }
    const isFirstLoad = rawData === null;
    if (!isFirstLoad && d.all_models.join() !== rawData.all_models.join()) {
      // A scan landed new models: rebuild the filter, keeping an explicit selection
      // (or selecting everything if the user had not narrowed it).
      const prev = rawData.all_models;
      const wasAll = prev.every(m => selectedModels.has(m));
      buildFilterUI(d.all_models, new Set(wasAll ? d.all_models : d.all_models.filter(m => selectedModels.has(m))));
    }
    rawData = d;
    renderMeta();

    if (isFirstLoad) {
      // Restore active tab from URL and show its panel before the first paint.
      const start = readURLTab();
      activeTab = start.tab;
      startHash = start.hash;
      applyTabVisibility(activeTab);
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Mark the active TZ button (displayTZ was read from the URL pre-fetch)
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === displayTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      buildSessionFilterUI(d.sessions_all || []);
      readSessionFilters();
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
    if (isFirstLoad) {
      const sessionId = readURLSession();
      if (sessionId) openSessionDetail(sessionId);
      else { updateURL(startHash); gotoSection(startHash); }  // rewrites a legacy ?tab= to its canonical form
    }
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
/** Auto-refresh is opt-in: the Settings checkbox is unchecked by default. */
function autoRefreshOn() {
  const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('autorefresh-cb'));
  return !!(cb && cb.checked);
}
function renderMeta() {
  if (!rawData) return;
  const note = (autoRefreshOn() && rangeIncludesToday(selectedRange)) ? '<br>Auto-rescan every 30s' : '';
  document.getElementById('meta').innerHTML = 'Updated: ' + esc(rawData.generated_at) + note;
}
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  // Rescan, not just reload: nothing else writes the DB after startup, so a
  // bare reload would re-read the same rows forever.
  if (autoRefreshOn() && rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(triggerRescan, 30000);
  }
}
function onAutoRefreshToggle() {
  scheduleAutoRefresh();
  renderMeta();
}

// ── Inline-handler shim ──────────────────────────────────────────────────
// index.html's on*="fn(...)" attributes resolve against the global scope, which
// an ES module is NOT part of. Expose exactly the handlers the markup references.
Object.assign(window, {
  triggerRescan, selectAllModels, clearAllModels, setRange, setDisplayTZ,
  setModelSort, resetPricing, onPricingChange, exportSessionsCSV,
  onSessionFilterChange, clearSessionFilters, setSessionSort, exportProjectsCSV,
  exportProjectBranchCSV, setProjectSort, setProjectBranchSort,
  closeSessionDetail, exportSessionTurnsCSV, closeToolPanel, onModelToggle, setGroupModels,
  openSessionDetail, openToolPanel, exportModelCSV,
  moreModelRows, lessModelRows, moreSessionRows, lessSessionRows,
  moreProjectRows, lessProjectRows, moreBranchRows, lessBranchRows,
  onAutoRefreshToggle, toggleRail, setThemePref, setDefaultTZ, clearProjectChip,
});
window.setTheme = setThemePref;

// ── Init ─────────────────────────────────────────────────────────────────
// Pricing must load before the first render so costs are correct. The table is
// fetched from the server (single source); failure degrades to empty pricing
// (costs show as $0 / N/A) rather than blocking the dashboard.
async function init() {
  try {
    const table = await fetchPricing();
    applyDefaultPricing(table);
  } catch (e) {
    console.error('Failed to load pricing from /api/pricing', e);
  }
  loadPricing();
  initTheme(() => renderTab(activeTab));  // palette changed: refill tokens + redraw the live screen
  initRail();
  syncDefaultTZButtons();
  renderPricingEditor();
  // Before the first fetch: the server buckets days in this timezone.
  displayTZ = readURLTZ();
  await loadExtensionTabs();  // before readURLTab/wireTabs so ?tab=<ext> resolves
  wireTabs();
  wireProjectExpand();
  wireSessionExpand();
  loadData();
  refreshStatus();
  scheduleAutoRefresh();
}
init();
