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
  isBillable, isEstimated, getPricing, calcCost, rowCost, rowCostBreakdown,
  sessionCost, sessionCostBreakdown, sessionCostAtListRates,
  cacheShare, coldRestarts, toolBreakdown,
} from './pricing.js';
import { VALID_TABS, resolveTab, applyTabVisibility, addExtRailItem, toggleRail, initRail } from './nav.js';
import { store, TZ_KEY, alpha, fillTokens, initTheme, setThemePref } from './theme.js';
import { weekOf, promptBands, turnsOfBand, sessionWaterfall, causeWeekMatrix } from './analytics.js';
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
let traceHighlight = new Set();  // message_ids of a finding's evidence calls, marked in the trace table
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
  document.querySelectorAll('.range-btn[data-range]').forEach(btn =>
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
    acc.prompt += (r.input || 0) + (r.cache_read || 0) + (r.cache_creation || 0);  // same basis as cacheShare()
    acc.cache_tokens += r.cache_read || 0;
    return acc;
  }, { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0, prompt: 0, cache_tokens: 0 });
}

function sessionSignals(sessions) {
  return sessions.map(s => {
    const cost = sessionCost(s, s.last_date);
    const minutes = Math.max(parseFloat(s.duration_min) || 0, 1 / 60);
    const tokens = (s.input || 0) + (s.output || 0) + (s.cache_read || 0) + (s.cache_creation || 0);
    return {
      ...s,
      cost,
      cost_per_min: cost / minutes,
      tokens_per_min: tokens / minutes,
      output_per_turn: s.turns ? (s.output || 0) / s.turns : 0,
      cache_share: cacheShare(s),
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
  uncached_input: 'Uncached input', cache_miss_rebuild: 'Unexpected cache misses',
};
// Finding drawer: what happened, the fix, the evidence calls as a mini-trace, then the full
// trace with those calls highlighted. Evidence message_ids ride along on the button.
function evidenceDrawer(s, fix) {
  const ev = s.evidence || [], ids = ev.map(e => e.message_id).filter(Boolean);
  return `<tr class="wg-drawer"><td colspan="5"><details><summary class="muted">Evidence: ${ev.length} call${ev.length === 1 ? '' : 's'}</summary>` +
    `<p>${esc(s.detail)}</p><p class="wg-fix"><strong>Fix:</strong> ${esc(fix)}</p>` +
    '<table><thead><tr><th>When</th><th>Tool</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>' +
    ev.map(e => `<tr><td class="muted">${esc(String(e.at || '').replace('T', ' ').replace('Z', ''))}</td><td>${esc(e.tool || '')}</td>` +
      `<td class="num">${fmt(e.tokens)}</td><td class="num">${fmtCostBig(e.usd)}</td></tr>`).join('') +
    `</tbody></table><button class="filter-btn" data-session="${esc(s.id)}" data-ids="${esc(ids.join(','))}"` +
    ` onclick="openSessionDetail(this.dataset.session, this.dataset.ids)">Open full trace</button></details></td></tr>`;
}

// Group findings into what a user acts on: a habit = (project, cause), not one session.
// Only findings from `sessions` (the list every other tab shows: model, range and project
// filters applied) count. `cause` narrows the habits; the scope and cause bars stay whole
// so the bars can still be clicked to switch cause. A cause no longer in scope (the range
// or a filter changed) is dropped, and the result's `cause` says which filter applied.
function groupWaste(findings, sessions, cause = null) {
  const byId = new Map(sessions.map(s => [s.full_session_id, s]));
  const inScope = findings.filter(f => byId.has(f.session_id));
  const spend = new Map(inScope.map(f => [f.session_id, f.session_usd]));
  const causes = new Map(), groups = new Map();
  for (const f of inScope) {
    const c = causes.get(f.cause) || { cause: f.cause, tier: f.tier, usd: 0, sessions: 0 };
    c.usd += f.usd; c.sessions++;
    if (c.tier !== f.tier) c.tier = 'inferred';  // mixed sources: show the weaker tier
    causes.set(f.cause, c);
  }
  if (!causes.has(cause)) cause = null;
  for (const f of inScope) {
    if (cause && f.cause !== cause) continue;
    const s = byId.get(f.session_id), key = s.project + '\u0000' + f.cause;
    const g = groups.get(key) || { project: s.project, cause: f.cause, tier: f.tier, fix: f.fix, usd: 0, spend: 0, sessions: [] };
    g.usd += f.usd; g.spend += f.session_usd;
    if (g.tier !== f.tier) g.tier = 'inferred';
    g.sessions.push({ id: f.session_id, title: s.topic || '', date: s.last_date, usd: f.usd,
                      session_usd: f.session_usd, detail: f.detail, evidence: f.evidence });
    groups.set(key, g);
  }
  const byUsd = (a, b) => b.usd - a.usd;
  for (const g of groups.values()) g.sessions.sort(byUsd);
  const total = inScope.reduce((sum, f) => sum + f.usd, 0);
  for (const c of causes.values()) c.share = total ? c.usd / total : 0;  // of all attributed waste
  return {
    cause,
    total,
    affectedUsd: [...spend.values()].reduce((sum, v) => sum + v, 0),
    affectedSessions: spend.size,
    causes: [...causes.values()].sort(byUsd),
    // ties: the habit spanning more sessions first, then by project name (stable across renders)
    groups: [...groups.values()].sort((a, b) => b.usd - a.usd || b.sessions.length - a.sessions.length
                                            || a.project.localeCompare(b.project)),
  };
}

// Weekly attributed waste as % of ALL spend that week (not just affected sessions), so busy and
// quiet weeks compare. Both sides bucket by the session's last_date week (Monday start), over
// the same `sessions` the rest of the tab uses; `costOf(session)` prices a session. Weeks
// between the first and last are filled with 0 so a gap reads as a gap.
function weeklyWaste(findings, sessions, costOf) {
  const weekOf = d => {
    const t = new Date(d + 'T00:00:00Z');
    t.setUTCDate(t.getUTCDate() - (t.getUTCDay() + 6) % 7);
    return t.toISOString().slice(0, 10);
  };
  const byId = new Map(sessions.map(s => [s.full_session_id, s]));
  const wk = new Map();
  const at = k => wk.get(k) || wk.set(k, { week: k, spend: 0, waste: 0 }).get(k);
  for (const s of sessions) if (s.last_date) at(weekOf(s.last_date)).spend += costOf(s);
  for (const f of findings) { const s = byId.get(f.session_id); if (s && s.last_date) at(weekOf(s.last_date)).waste += f.usd; }
  const keys = [...wk.keys()].sort();
  if (!keys.length) return [];
  const out = [];
  for (let k = keys[0]; k <= keys[keys.length - 1]; ) {
    const w = wk.get(k) || { week: k, spend: 0, waste: 0 };
    out.push({ ...w, pct: w.spend ? w.waste / w.spend : 0 });
    const t = new Date(k + 'T00:00:00Z'); t.setUTCDate(t.getUTCDate() + 7); k = t.toISOString().slice(0, 10);
  }
  return out;
}

function wasteSparkline(weeks) {
  if (weeks.length < 2) return '';
  const W = 240, H = 36, max = Math.max(...weeks.map(w => w.pct)) || 1;
  const pts = weeks.map((w, i) => [(i * W / (weeks.length - 1)).toFixed(1), (H - 3 - (H - 6) * w.pct / max).toFixed(1)]);
  return `<svg class="waste-spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" ` +
    `aria-label="Weekly attributed waste as a share of all spend, ${esc(weeks[0].week)} to ${esc(weeks[weeks.length - 1].week)}">` +
    `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts.map(p => p.join(',')).join(' ')}"/>` +
    pts.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="2"><title>Week of ${esc(weeks[i].week)}: ${fmtPct(weeks[i].pct)} ` +
      `(${fmtCostBig(weeks[i].waste)} of ${fmtCostBig(weeks[i].spend)})</title></circle>`).join('') + '</svg>' +
    `<div class="muted">Weekly attributed waste, % of all spend that week (peak ${fmtPct(max)})</div>`;
}

let wastePayload = null;   // /api/waste is whole-DB; fetched once per data load, filtered here
let wasteGen = 0;          // bumped on every data load; a response from an older one is dropped
let wasteFetch = null;     // { gen, promise }: one request in flight per data load
let wasteCause = null;     // cause-bar filter
let wasteShowAll = false;  // habits beyond the first WASTE_PAGE
const WASTE_PAGE = 25;
async function setWasteCause(c) {
  wasteCause = wasteCause === c ? null : c;
  wasteShowAll = false;
  await renderWasteTab();
  // the bars were re-rendered: put keyboard focus back on the one just activated
  document.querySelector(`.waste-bar[data-cause="${CSS.escape(c)}"]`)?.focus();
}
// Same fetch rules as renderWasteTab (one request per data load, stale responses dropped), for other screens.
async function loadWaste() {
  if (wastePayload) return wastePayload;
  const gen = wasteGen;
  if (!wasteFetch || wasteFetch.gen !== gen) wasteFetch = { gen, promise: fetchWaste() };
  const req = wasteFetch;
  try { const p = await req.promise; if (gen !== wasteGen) return null; return (wastePayload = p); }
  catch (e) { if (wasteFetch === req) wasteFetch = null; return null; }
}
let wasteWeek = null;      // heatmap-cell filter: only sessions whose last_date falls in this week
async function setWasteCell(c, week) {
  const clear = wasteCause === c && wasteWeek === week;
  wasteCause = clear ? null : c; wasteWeek = clear ? null : week; wasteShowAll = false;
  await renderWasteTab();
}
function clearWasteWeek() { wasteWeek = null; renderWasteTab(); }
function wasteHeatmap(findings, sessions) {
  const m = causeWeekMatrix(findings, sessions);
  if (m.weeks.length < 1) return '<p class="muted">Nothing to show.</p>';
  const cellBtn = (c, w, usd) => `<td><button class="hm-cell${wasteCause === c && wasteWeek === w ? ' active' : ''}" data-cause="${esc(c)}" data-week="${esc(w)}"` +
    ` onclick="setWasteCell(this.dataset.cause, this.dataset.week)" title="${esc(CAUSE_LABEL[c] || c)}, week of ${esc(w)}: ${fmtCostBig(usd)}"` +
    ` aria-label="${esc(CAUSE_LABEL[c] || c)}, week of ${esc(w)}: ${fmtCostBig(usd)}"><span class="hm-fill" style="opacity:${usd && m.max ? (0.12 + 0.88 * usd / m.max).toFixed(2) : 0}"></span></button></td>`;
  return '<div class="hm-scroll"><table class="hm"><thead><tr><th></th>' + m.weeks.map(w => `<th title="Week of ${esc(w)}">${esc(w.slice(5))}</th>`).join('') + '</tr></thead><tbody>' +
    m.rows.map(r => `<tr><th scope="row">${esc(CAUSE_LABEL[r.cause] || r.cause)}</th>` + r.cells.map((u, i) => cellBtn(r.cause, m.weeks[i], u)).join('') + '</tr>').join('') +
    '</tbody></table></div><div class="muted">Attributed $ per cause and week (Monday start), darker = more. Shade is relative to the biggest cell.</div>';
}
function showAllWaste() { wasteShowAll = true; renderWasteTab(); }

async function renderWasteTab() {
  const el = id => document.getElementById(id);
  if (!wastePayload) {
    const gen = wasteGen;
    if (!wasteFetch || wasteFetch.gen !== gen) wasteFetch = { gen, promise: fetchWaste() };
    let payload = null;
    const req = wasteFetch;
    // a failed request is forgotten so the next render retries, unless a newer one replaced it
    try { payload = await req.promise; } catch (e) { if (wasteFetch === req) wasteFetch = null; }
    if (gen !== wasteGen) return;  // the data reloaded meanwhile; that load renders again
    wastePayload = payload;
  }
  const w = wastePayload;
  if (!w) {  // never leave an earlier load's numbers on screen as if current
    el('waste-scope').textContent = 'Failed to load waste data.';
    for (const id of ['waste-bars', 'waste-heatmap', 'waste-groups', 'waste-measure', 'waste-measure-line']) el(id).textContent = '';
    return;
  }
  const label = c => CAUSE_LABEL[c] || c;
  const weekSessions = wasteWeek ? lastFilteredSessions.filter(s => s.last_date && weekOf(s.last_date) === wasteWeek) : lastFilteredSessions;
  const v = groupWaste(w.findings, weekSessions, wasteCause);
  // a cause that left the scope stops filtering, except under a week filter: an empty cell stays empty
  if (wasteWeek && wasteCause && !v.cause) v.groups = [];
  else wasteCause = v.cause;
  const { start, end } = getRangeBounds(selectedRange);

  // Scope: what was searched, what was found, against what spend.
  el('waste-scope').innerHTML = v.affectedSessions
    ? `<div class="waste-headline">${fmtCostBig(v.total)} attributed to waste</div>` +
      `<div class="muted">${fmtPct(v.affectedUsd ? v.total / v.affectedUsd : 0)} of the ${fmtCostBig(v.affectedUsd)} spent in the ` +
      `${v.affectedSessions} affected session${v.affectedSessions === 1 ? '' : 's'} · ${esc(RANGE_LABELS[selectedRange] || selectedRange)}` +
      `${start ? ' (' + esc(start) + (end ? ' to ' + esc(end) : ' to today') + ')' : ''} · current model and project filters</div>` +
      wasteSparkline(weeklyWaste(w.findings, lastFilteredSessions, x => sessionCostAtListRates(x, x.last_date)))
    : `<div class="muted">No material waste in ${esc(RANGE_LABELS[selectedRange] || selectedRange)} for the current filters.</div>`;
  el('waste-basis').textContent = 'Priced at ' + w.basis.pricing + ' (' + w.basis.label + '). Local price overrides do not apply here.';

  // Measurement status: one line; the per-cause matrix only behind the expander.
  const m = w.measurement, causesM = Object.keys(m.causes);
  const measured = m.sources.filter(s => causesM.every(c => m.causes[c][s].status === 'measured'));
  const not = m.sources.filter(s => !measured.includes(s));
  el('waste-measure-line').textContent = 'Measured for: ' + (measured.join(', ') || 'none') +
    (not.length ? ' · Not fully measurable: ' + not.join(', ') + ' (why?)' : '');
  el('waste-measure').innerHTML = not.length ? '<tbody>' + not.map(s => {
    const reasons = [...new Set(causesM.filter(c => m.causes[c][s].status !== 'measured').map(c => {
      const x = m.causes[c][s];
      return x.status === 'inferred' ? label(c) + ': inferred (' + x.reason + ')' : x.reason;
    }))];
    return `<tr><td>${esc(s)}</td><td class="muted">${reasons.map(esc).join('<br>')}</td></tr>`;
  }).join('') + '</tbody>' : '';
  el('waste-measure-wrap').classList.toggle('plain', !not.length);  // coverage stays visible; no reasons to expand

  // Cause bars: click one to narrow the habits below; click again to clear.
  const top = v.causes.length ? v.causes[0].usd : 0;
  el('waste-bars').innerHTML = v.causes.map(c =>
    `<button class="waste-bar${wasteCause === c.cause ? ' active' : ''}" data-cause="${esc(c.cause)}" onclick="setWasteCause(this.dataset.cause)"` +
    ` aria-pressed="${wasteCause === c.cause}" title="Evidence: ${esc(c.tier)}. Click to show only this cause below.">` +
    `<span class="wb-label">${esc(label(c.cause))}</span>` +
    `<span class="wb-track"><span class="wb-fill" style="width:${top ? (100 * c.usd / top).toFixed(1) : 0}%"></span></span>` +
    `<span class="wb-val">${fmtCostBig(c.usd)} <span class="muted">· ${fmtPct(c.share)} · ${c.sessions} session${c.sessions === 1 ? '' : 's'}` +
    // most causes are "attributed cause"; flag only the weaker tier on the bar itself
    `${c.tier === 'attributed cause' ? '' : ' · ' + esc(c.tier)}</span></span></button>`
  ).join('') || '<p class="muted">Nothing to show.</p>';

  // Heatmap shows every week; the week filter only narrows the bars and habits.
  el('waste-heatmap').innerHTML = wasteHeatmap(w.findings, lastFilteredSessions) +
    (wasteWeek ? `<div>Week of ${esc(wasteWeek)} only <button class="filter-btn" onclick="clearWasteWeek()">Clear week</button></div>` : '');

  // Habits: (project, cause), biggest first; sessions inside, each opening its trace.
  const shown = wasteShowAll ? v.groups : v.groups.slice(0, WASTE_PAGE);
  el('waste-groups-title').textContent = 'Habits: project × cause' + (wasteCause ? ' — ' + label(wasteCause) : '');
  el('waste-groups').innerHTML = shown.map(g =>
    `<details class="waste-group"><summary>` +
    `<span class="wg-project">${esc(g.project)}</span><span>${esc(label(g.cause))}</span>` +
    `<span class="num">${fmtCostBig(g.usd)}</span>` +
    `<span class="muted num">${g.sessions.length} session${g.sessions.length === 1 ? '' : 's'} · ${fmtPct(g.spend ? g.usd / g.spend : 0)} of their spend</span>` +
    `</summary><p class="wg-fix"><strong>Fix:</strong> ${esc(g.fix)} <span class="muted">(${esc(g.tier)})</span></p>` +
    '<table><thead><tr><th>Last active</th><th>Session</th><th>Attributed</th><th>Of session</th><th>What happened</th></tr></thead><tbody>' +
    g.sessions.map(s => `<tr><td class="muted">${esc(s.date || '')}</td>` +
      `<td><button class="link-btn" data-session="${esc(s.id)}" onclick="openSessionDetail(this.dataset.session)" title="Open trace">` +
      `${esc(s.title || s.id.slice(0, 8) + '…')}</button></td>` +
      `<td class="num">${fmtCostBig(s.usd)}</td><td class="num muted">${fmtCostBig(s.session_usd)}</td>` +
      `<td class="muted">${esc(s.detail)}</td></tr>` + evidenceDrawer(s, g.fix)).join('') +
    '</tbody></table></details>'
  ).join('') + (shown.length < v.groups.length
    ? `<button class="filter-btn" onclick="showAllWaste()">Show all ${v.groups.length} habits</button>` : '')
    || '<p class="muted">No material waste found.</p>';
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
  const cacheShare = c.prompt ? c.cache_tokens / c.prompt : 0;
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
  const cold = coldRestarts(d.turns);
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
    ['Cache Share', fmtPct(cacheShare(s))],
    ['Cache Savings', fmtCost(c.cache_savings)],
    ['Cache Writes', fmtCost(c.cache_creation)],
    ['Cold Restarts', cold.count ? `${cold.count} · ${fmt(cold.tokens)} tok · ${fmtCost(cold.cost)}` : '0'],
  ].map(([label, value]) => `<div class="detail-stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
  document.getElementById('session-detail-body').innerHTML = turnsOfBand(d.turns, traceBand).map(({ t, i }) => {
    const cost = rowCost(t, t.timestamp.slice(0, 10));
    return `<tr${traceHighlight.has(t.message_id) ? ' class="hl" aria-label="evidence call"' : ''}>
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
  renderSessionBands(d);
  renderSessionToolBreakdown(turnsOfBand(d.turns, traceBand).map(x => x.t));
  renderSessionWaterfall(d);
  renderSessionTimeline(d.turns);
}

// ── Trace: request bands and cost waterfall ──────────────────
let traceBand = null;  // prompt_seq filtering the turn and tool tables, or null
function setTraceBand(seq) {
  traceBand = traceBand === seq ? null : seq;
  if (selectedSessionDetail) renderSessionDetail(selectedSessionDetail);
  document.querySelector(`.req-band[data-seq="${traceBand}"]`)?.focus();
}
function renderSessionBands(d) {
  const bands = promptBands(d.turns, t => rowCost(t, t.timestamp.slice(0, 10)));
  if (!bands.some(b => b.seq === traceBand)) traceBand = null;  // another session's band
  document.getElementById('session-bands-card').classList.toggle('hidden', !bands.length);
  document.getElementById('session-tool-title').textContent = 'Tool Breakdown' + (traceBand ? ' — request ' + traceBand : '');
  const top = Math.max(...bands.map(b => b.cost), 0);
  document.getElementById('session-bands').innerHTML = bands.map(b =>
    `<button class="waste-bar req-band${traceBand === b.seq ? ' active' : ''}" data-seq="${b.seq}" onclick="setTraceBand(+this.dataset.seq)" aria-pressed="${traceBand === b.seq}">` +
    `<span class="wb-label">Request ${b.seq}</span>` +
    `<span class="wb-track"><span class="wb-fill" style="width:${top ? (100 * b.cost / top).toFixed(1) : 0}%"></span></span>` +
    `<span class="wb-val">${b.calls} call${b.calls === 1 ? '' : 's'} → ${fmtCost(b.cost)}</span></button>`).join('');
}
// Waterfall of waste findings for the open session; needs /api/waste, so it fills in async.
async function renderSessionWaterfall(d) {
  const card = document.getElementById('session-waterfall-card');
  card.classList.add('hidden');
  const w = await loadWaste();
  if (!w || selectedSessionDetail !== d) return;  // failed, or another session opened meanwhile
  const wf = sessionWaterfall(w.findings, d.session.session_id,
    sessionCostAtListRates({ model: d.session.model, by_model: d.turns }, (d.session.last || '').slice(0, 10)));
  if (!wf) return;
  const rows = [['Session total', 0, wf.total],
    ...wf.steps.map((x, i, a) => [CAUSE_LABEL[x.cause] || x.cause, a.slice(0, i).reduce((s, y) => s + y.usd, 0), x.usd]),
    ['Unattributed', wf.attributed, wf.unattributed]];
  const span = Math.max(wf.total, wf.attributed) || 1;
  document.getElementById('session-waterfall').innerHTML = rows.map(([label, from, usd]) =>
    `<div class="waste-bar wf-row"><span class="wb-label">${esc(label)}</span>` +
    `<span class="wb-track"><span class="wb-fill" style="margin-left:${(100 * from / span).toFixed(1)}%;width:${(100 * usd / span).toFixed(1)}%"></span></span>` +
    `<span class="wb-val">${fmtCostBig(usd)}</span></div>`).join('') +
    (wf.over ? '<p class="muted">Attributions overlap: they add up to more than the session total.</p>' : '');
  card.classList.remove('hidden');
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
  const rows = toolBreakdown(turns);
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

// ── Session timeline: context panel above, cost panel below, one shared x axis ──
// x is elapsed seconds (or call number), so idle gaps show as width, not as one more bar.
// Zoom is one shared x range; both panels follow it.
const TIMELINE_GAP_S = 300;  // the 5-minute cache TTL: a longer gap may force a rebuild
let timelineX = 'elapsed';   // 'elapsed' | 'clock' | 'call'
let timelineLog = false;
let timelineZoom = null;     // { min, max } in x units, or null
let timelineBounds = null;   // the session's own x range: the axis stops at the data
let timelineTurns = null;    // the turns the zoom belongs to: another session starts unzoomed

function fmtElapsed(sec) {
  if (sec < 0) return '-' + fmtElapsed(-sec);  // a row whose time is earlier than the first call's
  if (sec > 0 && sec < 60 && sec % 1) return sec.toFixed(1) + 's';  // sub-second zoom
  const s = Math.max(0, Math.round(sec)), d = Math.floor(s / 86400);
  const h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  const p = n => String(n).padStart(2, '0');
  return d ? `${d}d${p(h)}h` : h ? `${h}h${p(m)}m` : m ? (s % 60 ? `${m}m${p(s % 60)}s` : `${m}m`) : `${s}s`;
}

// Round tick steps (1m, 5m, 1h...). Clock mode aligns them to wall time via `origin` (epoch s).
const TICK_STEPS = [0.1, 0.2, 0.5, 1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
                    172800, 604800, 1209600, 2592000];  // resumed sessions can span weeks
function timeTicks(min, max, origin, minStep = 0.1) {
  const step = TICK_STEPS.find(s => s >= minStep && (max - min) / s <= 8) || TICK_STEPS[TICK_STEPS.length - 1];
  const ticks = [];
  // integer multiples, so 0.1-second steps do not accumulate float error
  for (let k = Math.ceil((origin + min) / step - 1e-9); k * step - origin <= max + 1e-9; k++) {
    ticks.push({ value: Math.round((k * step - origin) * 1000) / 1000 });
  }
  // a range narrower than the smallest step still gets its two ends labelled
  return ticks.length >= 2 ? ticks : [{ value: min }, { value: max }];
}

// Epoch ms of the first call with a parseable `ts` (undefined if none: only Call # works then).
function timelineOrigin(turns) {
  return turns.map(t => Date.parse(t.ts)).find(Number.isFinite);
}

// One x per call: its true seconds since the first dated call. An undated call sits at the
// previous dated one. Calls may share an x (advisor rows inherit their parent's timestamp):
// the cost panel draws floating bars from timelineCostBars, so they never stack together.
function timelineXs(turns) {
  const t0 = timelineOrigin(turns);
  if (timelineX === 'call' || t0 === undefined) return turns.map((t, i) => i + 1);
  let last = t0;
  return turns.map(t => {
    const ms = Date.parse(t.ts);
    if (Number.isFinite(ms)) last = ms;
    return (last - t0) / 1000;
  });
}

// Per cost part, one [bottom, top] floating bar per call, stacked within that call only.
// Chart.js's own stacking sums every bar at the same x, merging calls that share a time.
// On a log axis the floor is LOG_FLOOR and a part that ends below it is not drawn.
const COST_PARTS = ['cache_creation', 'cache_read', 'output', 'input'];
const COST_LABELS = { cache_creation: 'Cache write', cache_read: 'Cache read', output: 'Output', input: 'Input' };
const LOG_FLOOR = 0.001;
function timelineCostBars(parts, log) {
  const bars = Object.fromEntries(COST_PARTS.map(k => [k, []]));
  for (const p of parts) {
    let base = 0;
    for (const k of COST_PARTS) {
      const top = base + (p[k] || 0);
      const lo = log ? Math.max(base, LOG_FLOOR) : base;
      bars[k].push(p[k] > 0 && top > lo ? [lo, top] : null);
      base = top;
    }
  }
  return bars;
}

// [x0, x1] spans between consecutive DATED calls at least TIMELINE_GAP_S apart.
function timelineGaps(turns, xs) {
  const gaps = [];
  let prev = null;
  turns.forEach((t, i) => {
    const ms = Date.parse(t.ts);
    if (!Number.isFinite(ms)) return;
    if (prev && (ms - prev.ms) / 1000 >= TIMELINE_GAP_S) gaps.push([prev.x, xs[i]]);
    prev = { ms, x: xs[i] };
  });
  return gaps;
}

// Indices of calls whose actor's context fell below 60% of its previous call. Compaction:
// turns.is_compact_summary is never set on assistant rows (Claude Code flags the user
// record), so mark what a compaction looks like, plus the flag where it is set.
function timelineDrops(turns) {
  const last = {};
  return turns.flatMap((t, i) => {
    const prev = last[t.lane];  // per lane: two agents of one type are separate contexts
    last[t.lane] = t.context;
    return t.is_compact_summary || (prev > 0 && t.context < prev * 0.6) ? [i] : [];
  });
}

// The x range to show: every position, not first..last (calls are not always in time order).
function timelineRange(xs) {
  let min = Infinity, max = -Infinity;
  for (const x of xs) { if (x < min) min = x; if (x > max) max = x; }
  return { min, max: Math.max(max, min + 1) };
}

// Every call sharing call i's x position (on a time axis they overlap), at most `cap` of them.
// Chart.js's own 'x'/'index' modes pick elements by pixel and miss most of a dense stack.
function timelineCallsAt(xs, i, cap = 6) {
  const all = [];
  xs.forEach((x, k) => { if (x === xs[i]) all.push(k); });
  return { shown: all.slice(0, cap), more: Math.max(0, all.length - cap), all };
}

function timelineTick(turns) {
  const t0 = timelineOrigin(turns);
  if (timelineX === 'call') return v => Number.isInteger(v) ? '#' + v : '';
  if (timelineX === 'clock') return v => {
    const d = new Date(t0 + v * 1000), sec = d.getUTCSeconds();  // zone offsets are whole minutes
    return fmtLocalDateTime(d.toISOString(), displayTZ).slice(11) + (sec ? ':' + String(sec).padStart(2, '0') : '');
  };
  return v => fmtElapsed(v);
}

// Gap bands and the drag-to-zoom rectangle, shared by both panels.
const timelinePlugin = {
  id: 'timelineOverlay',
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    ctx.fillStyle = alpha(C.amber, 0.14);
    for (const [x0, x1] of chart.canvas.$gaps || []) {
      const p0 = Math.max(a.left, x.getPixelForValue(x0)), p1 = Math.min(a.right, x.getPixelForValue(x1));
      if (p1 > a.left && p0 < a.right) ctx.fillRect(p0, a.top, Math.max(3, p1 - p0), a.bottom - a.top);
    }
    ctx.restore();
  },
  afterDraw(chart) {
    const d = chart.$drag;
    if (!d) return;
    const { ctx, chartArea: a } = chart;
    ctx.save();
    ctx.fillStyle = alpha(C.accent, 0.15);
    ctx.fillRect(Math.min(d.x0, d.x1), a.top, Math.abs(d.x1 - d.x0), a.bottom - a.top);
    ctx.restore();
  },
  afterEvent(chart, args) {
    const e = args.event, a = chart.chartArea;
    const x = Math.min(a.right, Math.max(a.left, e.x));
    if (e.type === 'mousedown' && e.x >= a.left && e.x <= a.right) {
      chart.$drag = { x0: x, x1: x };
    } else if (e.type === 'mousemove' && chart.$drag) {
      chart.$drag.x1 = x;
      args.changed = true;
    } else if ((e.type === 'mouseup' || e.type === 'mouseout') && chart.$drag) {
      const d = chart.$drag;
      chart.$drag = null;
      args.changed = true;
      if (Math.abs(d.x1 - d.x0) > 5) {
        const s = chart.scales.x;
        setTimelineZoom({ min: s.getValueForPixel(Math.min(d.x0, d.x1)), max: s.getValueForPixel(Math.max(d.x0, d.x1)) });
      }
    } else if (e.type === 'dblclick') {
      resetTimelineZoom();
    }
  },
};

function renderSessionTimeline(turns) {
  for (const k of ['sessionContext', 'sessionTimeline']) if (charts[k]) { charts[k].destroy(); charts[k] = null; }
  if (turns !== timelineTurns) { timelineTurns = turns; timelineZoom = null; }
  if (turns.length && timelineOrigin(turns) === undefined) timelineX = 'call';  // no usable timestamps
  document.querySelectorAll('[data-tlx]').forEach(b => b.classList.toggle('active', b.dataset.tlx === timelineX));
  document.querySelectorAll('[data-tly]').forEach(b => b.classList.toggle('active', (b.dataset.tly === 'log') === timelineLog));
  if (!turns.length) return;
  const xs = timelineXs(turns);
  timelineBounds = timelineRange(xs);
  const t0 = timelineOrigin(turns);
  const canvases = ['chart-session-context', 'chart-session-timeline'].map(id => document.getElementById(id));
  const gaps = timelineGaps(turns, xs);
  for (const c of canvases) c.$gaps = gaps;  // read by timelinePlugin, which draws during construction
  const callTitle = i => {
    const t = turns[i], ms = Date.parse(t.ts);
    return [`#${i + 1} · ${t.ts || 'undated'}` + (Number.isFinite(ms) ? ` · +${fmtElapsed((ms - t0) / 1000)}` : ''),
            `${t.actor} · ${t.tool || '<none>'}${t.estimated ? ' (est.)' : ''}`];
  };
  // Tooltips use only the nearest element to find a call, then read everything from that call.
  const first = (item, k) => k === 0;
  const title = items => {
    if (!items.length) return '';
    const at = timelineCallsAt(xs, items[0].raw.i);
    return at.all.length === 1 ? callTitle(at.all[0])
      : [`${at.all.length} calls at this point (Call # shows them apart)`];
  };
  // Several calls at one x: one line each (capped), valued by `value(i)`.
  const callLines = (at, value) => at.shown
    .map(i => ` ${callTitle(i)[0].split(' · ')[0]} · ${callTitle(i)[1]}: ${value(i)}`)
    .concat(at.more ? [` +${at.more} more`] : []);
  const xScale = () => ({
    type: 'linear', offset: false, min: (timelineZoom || timelineBounds).min, max: (timelineZoom || timelineBounds).max,
    ticks: { color: C.axis, maxRotation: 0, callback: timelineTick(turns) }, grid: { color: C.border },
    afterBuildTicks: axis => {
      if (timelineX !== 'call') axis.ticks = timeTicks(axis.min, axis.max, timelineX === 'clock' ? t0 / 1000 : 0, timelineX === 'clock' ? 1 : 0.1);
    },
  });
  const yWidth = axis => { axis.width = 64; };  // same gutter on both panels keeps their x axes aligned
  const events = ['mousedown', 'mousemove', 'mouseup', 'mouseout', 'click', 'dblclick'];
  const legend = { labels: { color: C.axis, boxWidth: 12, font: { size: 11 } } };
  const heading = text => ({ display: true, text, color: C.axis, align: 'start' });

  // Context panel: one stepped line per actor (main + each subagent), compactions marked.
  const byActor = new Map();  // one lane per agent_id (server-labelled), so two Explore agents are two lines
  turns.forEach((t, i) => {
    if (!byActor.has(t.lane)) byActor.set(t.lane, []);
    byActor.get(t.lane).push({ x: xs[i], y: t.context, i });
  });
  const ctxSets = [...byActor].map(([actor, data], k) => {
    const col = actor === 'main' ? C.text : MODEL_COLORS[k % MODEL_COLORS.length];  // main stands apart from agents
    return {
      label: actor, type: 'line', stepped: 'before', borderWidth: 1.5, pointRadius: 0, pointHitRadius: 4,
      borderColor: col, backgroundColor: col,
      data,
    };
  });
  const compact = timelineDrops(turns).map(i => ({ x: xs[i], y: turns[i].context, i }));
  if (compact.length) ctxSets.push({ label: 'Context drop', type: 'scatter', data: compact, pointStyle: 'triangle',
                                     pointRadius: 7, borderColor: C.red, backgroundColor: C.red });
  charts.sessionContext = new Chart(canvases[0].getContext('2d'), {
    data: { datasets: ctxSets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, events,
      interaction: { mode: 'nearest', intersect: false, axis: 'x' },
      plugins: {
        title: heading('Context size per call: tokens the model re-reads every call'),
        legend,
        tooltip: { filter: first, callbacks: { title, label: item => {
          const at = timelineCallsAt(xs, item.raw.i);
          return at.all.length === 1 ? ` ${item.dataset.label}: ${fmt(item.parsed.y)} tokens`
            : callLines(at, i => `${fmt(turns[i].context)} tokens`);
        } } },
      },
      scales: { x: xScale(), y: { beginAtZero: true, afterFit: yWidth, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } } },
    },
    plugins: [timelinePlugin],
  });

  // Cost panel: $ per call, split by what it paid for.
  const parts = turns.map(t => rowCostBreakdown(t, t.timestamp.slice(0, 10)));
  const width = Math.max(1, Math.min(10, 900 / turns.length));
  const bars = timelineCostBars(parts, timelineLog);
  const costSet = (label, key, color) => ({
    label, key, type: 'bar', backgroundColor: color, barThickness: width, grouped: false,
    data: bars[key].map((y, i) => ({ x: xs[i], y, i })),
  });
  charts.sessionTimeline = new Chart(canvases[1].getContext('2d'), {
    data: { datasets: [
      costSet('Cache write', 'cache_creation', TOKEN_COLORS.cache_creation),
      costSet('Cache read', 'cache_read', TOKEN_COLORS.cache_read),
      costSet('Output', 'output', TOKEN_COLORS.output),
      costSet('Input', 'input', TOKEN_COLORS.input),
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, events,
      interaction: { mode: 'nearest', intersect: false, axis: 'x' },
      plugins: {
        title: heading('Cost per call, by what it paid for (API list rate)'),
        legend,
        tooltip: {
          filter: first,
          callbacks: {
            title,
            label: item => {
              const at = timelineCallsAt(xs, item.raw.i);
              if (at.all.length === 1) {
                return COST_PARTS.filter(k => parts[item.raw.i][k] > 0)
                  .map(k => ` ${COST_LABELS[k]}: ${fmtCost(parts[item.raw.i][k])}`);
              }
              return callLines(at, i => fmtCost(parts[i].total));
            },
            footer: items => {
              if (!items.length) return '';
              const at = timelineCallsAt(xs, items[0].raw.i);
              return `Total: ${fmtCost(at.all.reduce((sum, i) => sum + parts[i].total, 0))}`;
            },
          },
        },
      },
      scales: {
        x: xScale(),
        y: {
          type: timelineLog ? 'logarithmic' : 'linear', beginAtZero: !timelineLog, afterFit: yWidth,
          min: timelineLog ? LOG_FLOOR : undefined,  // sub-tenth-of-a-cent slivers would stretch the log axis
          grid: { color: C.border },
          ticks: { color: C.axis, callback: v => !timelineLog ? '$' + Number(v).toFixed(2)
                     : Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9 ? '$' + Number(v) : '' },
        },
      },
    },
    plugins: [timelinePlugin],
  });
}

function setTimelineZoom(range) {
  timelineZoom = range;
  for (const c of [charts.sessionContext, charts.sessionTimeline]) {
    if (!c) continue;
    c.options.scales.x.min = (range || timelineBounds).min;
    c.options.scales.x.max = (range || timelineBounds).max;
    c.update('none');
  }
}
function resetTimelineZoom() { setTimelineZoom(null); }
function setTimelineX(mode) {
  timelineX = mode;
  timelineZoom = null;  // units differ between modes
  if (selectedSessionDetail) renderSessionTimeline(selectedSessionDetail.turns);
}
function setTimelineLog(on) {
  timelineLog = on;
  if (selectedSessionDetail) renderSessionTimeline(selectedSessionDetail.turns);
}

function closeSessionDetail() { setTab('sessions'); }

async function openSessionDetail(sessionId, highlightIds = '') {
  traceHighlight = new Set(String(highlightIds || '').split(',').filter(Boolean));  // callers without ids clear it
  try {
    const d = await fetchSession(sessionId);
    if (d.error) throw new Error(d.error);
    selectedSessionDetail = d;
    traceBand = null;  // a band belongs to one session
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
    wastePayload = null;  // findings follow the data: refetch on the next Waste render
    wasteGen++;
    renderMeta();

    if (isFirstLoad) {
      // Restore active tab from URL and show its panel before the first paint.
      const start = readURLTab();
      activeTab = start.tab;
      startHash = start.hash;
      applyTabVisibility(activeTab);
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn[data-range]').forEach(btn =>
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
  setTimelineX, setTimelineLog, resetTimelineZoom, setWasteCause, setWasteCell, clearWasteWeek, setTraceBand, showAllWaste,
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
