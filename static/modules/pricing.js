// @ts-check
// Client-side pricing: the price TABLE is fetched from the server (pricing.py via
// GET /api/pricing) so it is never hardcoded here. Only the cost *formula* below
// (tokens × rate / 1e6, summed) is intentionally re-implemented in JS, because the
// editable-pricing UI needs to recompute costs live in the browser. The Python
// `calc_cost` is the mirror of `calcCostBreakdown().total`.
//
// Effective-dated pricing: a DEFAULT_PRICING entry is either a flat rate dict, or
// (for a model whose rates change over time, e.g. claude-sonnet-5) an ascending
// list of tier dicts, each optionally carrying "from": "YYYY-MM-DD" (mirrors
// pricing.py). `resolveTier` picks the last tier whose "from" <= day.
// PRICING (the table backing the editor UI) always holds FLAT dicts — the tier
// active TODAY for tiered models — so the Pricing tab has one row of numbers to
// show/edit. Editing a field OVERRIDES the model with that flat dict for good:
// the override wins for every day, past or future (simple, predictable — no
// per-tier editing UI).

export const PRICING_STORAGE_KEY = 'claudeUsagePricingOverrides';

// Built-in defaults, populated from GET /api/pricing at startup (see
// applyDefaultPricing). Kept as one object whose contents are replaced in place
// so importing modules keep a valid live reference. Values are flat dicts OR
// tier-list arrays, as served by GET /api/pricing.
export const DEFAULT_PRICING = {};

// The effective table = today's tier of DEFAULT_PRICING, merged with the user's
// local overrides. Always flat dicts. Reassigned by loadPricing(); importers see
// the live value.
export let PRICING = {};

// model -> partial {input,output,cache_write,cache_read} the user edited locally.
let pricingOverrides = {};

function todayStr() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

/**
 * Resolve a DEFAULT_PRICING entry to the rate dict active on `day`. Flat entries
 * pass through unchanged; tier-list entries pick the last tier whose "from" is
 * <= day (tiers without "from" always qualify). Mirrors pricing.get_pricing().
 * @param {any} entry
 * @param {string} [day] YYYY-MM-DD; defaults to today.
 */
function resolveTier(entry, day) {
  if (!Array.isArray(entry)) return entry;
  const d = day || todayStr();
  let chosen = entry[0];
  for (const tier of entry) {
    if (!tier.from || tier.from <= d) chosen = tier;
  }
  return chosen;
}

/** Read access to the current local overrides (for the editor's Custom/Default tag). */
export function getOverrides() {
  return pricingOverrides;
}

/**
 * Replace DEFAULT_PRICING's contents in place with the server-supplied table,
 * preserving key/insertion order (drives the pricing editor's row order).
 * @param {Record<string, any>} table
 */
export function applyDefaultPricing(table) {
  for (const k of Object.keys(DEFAULT_PRICING)) delete DEFAULT_PRICING[k];
  Object.assign(DEFAULT_PRICING, table || {});
}

export function loadPricing() {
  try {
    pricingOverrides = JSON.parse(localStorage.getItem(PRICING_STORAGE_KEY) || '{}') || {};
  } catch(e) {
    pricingOverrides = {};
  }
  PRICING = {};
  for (const [key, entry] of Object.entries(DEFAULT_PRICING)) {
    PRICING[key] = { ...resolveTier(entry, todayStr()) };
  }
  for (const [model, override] of Object.entries(pricingOverrides)) {
    if (!PRICING[model]) continue;
    PRICING[model] = { ...PRICING[model], ...override };
  }
}

export function savePricing() {
  try { localStorage.setItem(PRICING_STORAGE_KEY, JSON.stringify(pricingOverrides)); } catch (e) { /* storage blocked: edits last for this page only */ }
}

/**
 * Apply a single price edit, mirroring the original onchange bookkeeping:
 * persist the override, update the effective PRICING, and drop the override
 * entirely if every edited field is back at its default. Returns false if the
 * input was invalid (caller should restore the displayed value).
 * @returns {boolean} whether the edit was accepted
 */
export function setOverride(model, field, value) {
  if (!DEFAULT_PRICING[model] || !field || !isFinite(value) || value < 0) {
    return false;
  }
  if (!pricingOverrides[model]) pricingOverrides[model] = {};
  pricingOverrides[model][field] = value;
  PRICING[model][field] = value;

  const defaults = resolveTier(DEFAULT_PRICING[model], todayStr());
  const overrides = pricingOverrides[model];
  if (Object.keys(overrides).every(k => overrides[k] === defaults[k])) {
    delete pricingOverrides[model];
  }
  savePricing();
  return true;
}

/** Clear all local overrides and rebuild PRICING from defaults (the Reset action). */
export function clearOverrides() {
  pricingOverrides = {};
  try { localStorage.removeItem(PRICING_STORAGE_KEY); } catch (e) { /* storage blocked */ }
  loadPricing();
}

/** Anthropic models (fable/opus/sonnet/haiku) billed at real API rates. */
export function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('fable') || m.includes('opus-') || m.includes('sonnet') || m.includes('haiku');
}

/**
 * Plan-included models (Codex / GPT-5 / GPT-6) whose cost is a labeled
 * API-equivalent estimate, not a real bill. Mirrors the codex/gpt-5/gpt-6
 * fallbacks in pricing.get_pricing() (Python) — keep the two families in sync.
 * NB this gates the cost CELL independently of whether a rate resolves: when
 * gpt-6-astra was priced but missing here, "cost by model" showed n/a while
 * "cost by effort level" showed the cost, for the same turns.
 */
export function isEstimated(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('codex') || m.includes('gpt-5') || m.includes('gpt-6');
}

// Resolve model -> DEFAULT_PRICING key (exact, then first-wins prefix, then
// keyword fallback). Mirrors pricing.get_pricing()'s key resolution.
function resolveKey(model) {
  if (!model) return null;
  if (DEFAULT_PRICING[model]) return model;
  for (const key of Object.keys(DEFAULT_PRICING)) {
    if (model.startsWith(key)) return key;
  }
  const m = model.toLowerCase();
  // 5.1 first: its cache-read rate is 0.025x input, not 0.1x (see pricing.py).
  if (m.includes('fable-5-1') || m.includes('mythos-5-1')) return 'claude-fable-5-1';
  if (m.includes('fable'))  return 'claude-fable-5';
  if (m.includes('opus-5-5')) return 'claude-opus-5-5'; // 0.05x cache reads, not 4.8's 0.1x
  if (m.includes('opus-'))  return 'claude-opus-4-8'; // "opus-" only: a model merely ending in "opus" is a local build, not billed
  if (m.includes('sonnet')) return 'claude-sonnet-4-6';
  if (m.includes('haiku-5-5')) return 'claude-haiku-5-5'; // 10x cheaper than 4.5
  if (m.includes('haiku'))  return 'claude-haiku-4-5';
  if (m.includes('codex'))  return 'gpt-5.3-codex';
  if (m.includes('gpt-5'))  return m.includes('mini') ? 'gpt-5.4-mini' : 'gpt-5.4';
  if (m.includes('gpt-6'))  return 'gpt-6-astra'; // unknown gpt-6 variant: top tier, not $0
  return null;
}

/**
 * Mirrors pricing.get_pricing() (Python): resolve model -> rate dict, honoring
 * effective-dated tiers. `day` (YYYY-MM-DD) picks the tier active on that day;
 * omit for "today". A local override always wins, regardless of `day` (it
 * replaced all tiers — see the module comment above).
 * @param {string} model
 * @param {string} [day]
 */
export function getPricing(model, day) {
  const key = resolveKey(model);
  if (!key) return null;
  if (pricingOverrides[key]) return PRICING[key];
  return resolveTier(DEFAULT_PRICING[key], day);
}

export function calcCost(model, inp, out, cacheRead, cacheCreation, day, cacheCreation1h = 0) {
  return calcCostBreakdown(model, inp, out, cacheRead, cacheCreation, day, cacheCreation1h).total;
}

/**
 * Breakdown of one API payload row ({model, input, output, cache_read,
 * cache_creation, cache_creation_1h}) plus its fast-mode / US-only / long-prompt surcharge `r.x`:
 * the premium carried as extra tokens (pricing.py surcharge_sql), priced at the
 * row's own rates -- exact because cost is linear in tokens, edited rates included.
 * Any client-side merge of rows must merge `x` too, or the premium is dropped.
 */
export function rowCostBreakdown(r, day) {
  const c = calcCostBreakdown(r.model, r.input, r.output, r.cache_read, r.cache_creation,
    day, r.cache_creation_1h);
  const x = r.x;
  if (!x) return c;
  const s = calcCostBreakdown(r.model, x.input, x.output, x.cache_read, x.cache_creation,
    day, x.cache_creation_1h);
  for (const k of Object.keys(c)) c[k] += s[k];
  return c;
}

export function rowCost(r, day) {
  return rowCostBreakdown(r, day).total;
}

/**
 * Per-model cost breakdown for a whole session, summed across the models its turns
 * actually used (s.by_model from the API). Pricing a session by its single `model`
 * label × summed tokens misprices any multi-model session (a subagent on a different
 * model, or a mid-session /model switch). Falls back to the single-model path when
 * no breakdown is present (older payloads / non-session rows).
 * `day` (YYYY-MM-DD) is the fallback day used for tier resolution when a
 * `by_model` entry has no `timestamp` of its own (per-turn entries, e.g. the
 * session-detail view's `d.turns`, use their own timestamp instead — exact
 * per-turn pricing, same invariant as the Python side).
 * @param {{model?: string, input?: number, output?: number, cache_read?: number, cache_creation?: number, by_model?: Array<any>}} s
 * @param {string} [day]
 */
export function sessionCostBreakdown(s, day) {
  const bm = s && s.by_model;
  if (Array.isArray(bm) && bm.length) {
    return bm.reduce((acc, m) => {
      const c = rowCostBreakdown(m, m.timestamp ? m.timestamp.slice(0, 10) : day);
      acc.input += c.input;
      acc.output += c.output;
      acc.cache_read += c.cache_read;
      acc.cache_creation += c.cache_creation;
      acc.total += c.total;
      acc.cache_savings += c.cache_savings;
      return acc;
    }, { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0 });
  }
  return rowCostBreakdown(s, day);
}

/** Total $ cost of a session, priced per-model (see sessionCostBreakdown). */
export function sessionCost(s, day) {
  return sessionCostBreakdown(s, day).total;
}

export function calcCostBreakdown(model, inp, out, cacheRead, cacheCreation, day, cacheCreation1h = 0) {
  const zero = { input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0, cache_savings: 0 };
  // Gate on getPricing (mirrors Python calc_cost): prices Anthropic models AND the
  // plan-included Codex/GPT estimate; non-priced models (local, gpt-4o) fall to zero.
  const p = getPricing(model, day);
  if (!p) return zero;
  // 1h-TTL cache writes bill at 2x input, not the 5m 1.25x. `cacheCreation1h` is the
  // portion of `cacheCreation` written at 1h; defaulting to 0 prices everything at the
  // 5m rate, which is right for payloads with no split (mirrors Python calc_cost).
  const cc1h = Math.min(cacheCreation1h || 0, cacheCreation || 0);
  const input = inp * p.input / 1e6;
  const output = out * p.output / 1e6;
  const cache_read = cacheRead * p.cache_read / 1e6;
  const cache_creation = ((cacheCreation - cc1h) * p.cache_write
    + cc1h * (p.cache_write_1h ?? p.cache_write)) / 1e6;
  const cache_savings = cacheRead * Math.max(p.input - p.cache_read, 0) / 1e6;
  return { input, output, cache_read, cache_creation, total: input + output + cache_read + cache_creation, cache_savings };
}
