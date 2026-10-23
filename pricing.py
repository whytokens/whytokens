"""
pricing.py - Single source of truth for the token pricing table.

Cost is computed per turn (each turn knows its own model) then summed; aggregating
tokens first and applying one price is wrong for multi-model sessions.

This table is the ONE place the price numbers live. `cli.py` imports it, and
`api.py` serves it to the frontend at `GET /api/pricing` so the dashboard never
hardcodes the numbers. The cost *formula* (tokens x rate / 1e6, summed) is
intentionally re-implemented in JS for live editable-pricing recompute; only the
table is single-sourced.

Prices are $ / MTok (per million tokens):
  * Anthropic API pricing as of October 2026.
  * OpenAI (Codex) API pricing as of July 2026, web-verified against
    developers.openai.com/api/docs/pricing and .../codex/pricing.

Effective-dated pricing: most PRICING values are a flat rate dict. A model whose
rates change over time instead maps to a LIST of tier dicts, ordered ascending,
each optionally carrying "from": "YYYY-MM-DD" (the first tier has no "from" —
it applies since forever). `get_pricing(model, timestamp)` resolves the tier
active on that timestamp's day (last tier whose "from" <= day); omit timestamp
for "now". Everything here stays JSON-serializable (groundwork for a future
pricing-auto-update feature).

Codex caveat: most Codex usage runs on a ChatGPT plan where tokens are *plan-
included*, not billed per token. The OpenAI rates below therefore power an
**API-equivalent estimate** (tokens-first, cost as a labeled
estimate), not a real bill. ``cache_read`` holds the cached-input rate.
``cache_write=0`` applies to pre-5.6 OpenAI models, which have no cache-creation
concept (cost = input + output + cache_read). GPT-5.6 introduced a cache-write
rate (1.25x input, verified against developers.openai.com/api/docs/pricing, July
2026) — kept here as future-proofing. Codex CLI 0.146.0 added a
``cache_write_input_tokens`` field to its usage envelope but has never populated
it (always 0 as of August 2026), so ``cache_creation_tokens`` is still stored as
0 for Codex and this rate is currently unused. schema.py holds a value-level
canary (KNOWN_CODEX_ZERO_ONLY_USAGE_KEYS) that fails `scan --doctor` the first
time that field is nonzero. This estimate also does NOT model GPT-5.6's
2x long-context (>272K input) pricing tier.
Reasoning tokens are already included in ``output`` and are never billed separately.

``cache_write`` is the 5-minute-TTL cache-creation rate (1.25x input).
``cache_write_1h`` is the 1-hour-TTL rate (2x input) — Claude Code writes most of
its cache at 1h, and the turns table splits the two
(``cache_creation_5m_tokens`` / ``cache_creation_1h_tokens``). Entries without a
``cache_write_1h`` key fall back to ``cache_write``, which keeps the OpenAI rows
(no 1h tier) correct.
"""

from datetime import datetime

PRICING = {
    # Fable/Mythos 5.1 price cache HITS at 0.025x input ($0.25), not the 0.1x
    # ($1.00) every other model uses — verified against
    # platform.claude.com/docs/en/about-claude/pricing, 2026-09-02. Listed above
    # the 5.0 rows on purpose: get_pricing()'s prefix scan is first-wins over
    # insertion order, so "claude-fable-5-1-<date>" must meet this key before
    # "claude-fable-5" can swallow it.
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50, "cache_write_1h": 20.00},  # shares Fable 5.1 pricing
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50, "cache_write_1h": 20.00},  # shares Fable 5 pricing
    # Opus 5.5 cuts Opus 5 by 20% AND prices cache hits at 0.05x input ($0.20),
    # not 0.1x — verified against platform.claude.com/docs/en/about-claude/pricing,
    # 2026-09-22. Must sit above "claude-opus-5": the prefix scan is first-wins,
    # so "claude-opus-5-5" would otherwise bill at Opus 5 rates (2.5x on cache reads).
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00, "cache_write_1h": 8.00},
    # Opus 5 is a drop-in at Opus 4.8's rates (verified against
    # platform.claude.com/docs/en/about-claude/pricing, August 2026). Listed
    # explicitly rather than left to the "opus-" substring fallback so it shows
    # as its own editable row in the Pricing tab and stays pinned if the 4.x
    # rates ever diverge.
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25, "cache_write_1h": 10.00},
    # Sonnet 5 is $2/$10 (platform.claude.com/docs/en/about-claude/pricing). Claude
    # Code's own /usage panel may price it at $3/$15; that gap is the panel's.
    # Sonnet 5.5 has the same rates (platform.claude.com/docs/en/about-claude/pricing,
    # 2026-09-30); listed so it is its own Pricing-tab row.
    # Cache reads halved to $0.10 from 2026-10-07 (anthropic.com/claude-haiku-5-5
    # announcement, same day); earlier turns keep the $0.20 tier.
    "claude-sonnet-5-5": [
        {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00},
        {"from": "2026-10-07", "input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50, "cache_write_1h": 4.00},
    ],
    "claude-sonnet-5":   {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    # Haiku 5.5 (2026-10-07, anthropic.com/claude-haiku-5-5; not yet on the
    # platform pricing page). cache_write_1h is derived (standard 2x input), not
    # published. These are the <=100K-prompt rates; prompts over 100K cost 5x on
    # every token class — applied per turn by the multiplier below (LONG_PROMPT).
    "claude-haiku-5-5":  {"input": 0.10, "output":  0.50, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.20},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25, "cache_write_1h": 2.00},
    # OpenAI / Codex. Order matters: the prefix match below is first-wins, so
    # longer/more-specific keys (e.g. gpt-5.4-mini) must precede their base
    # (gpt-5.4). GPT-5.6 cache writes are NOT free (1.25x input); older gpt-5.x
    # entries have no cache-creation concept and stay cache_write=0.
    #
    # gpt-6-astra (released 2026-09-03). It needs its own key: the substring
    # fallback tests "gpt-5", not "gpt-6", and unmatched means $0, so astra
    # sessions would otherwise be silently free in this report. CAVEAT the table cannot express:
    # past 272K input tokens the whole request bills 2x input / 1.5x output, so
    # a long-context astra turn is UNDER-counted here.
    "gpt-6-astra":       {"input": 10.00, "output": 50.00, "cache_read": 1.00,  "cache_write": 12.50},
    # gpt-6-sol / gpt-6-luna (released 2026-09-22). Same 272K long-context caveat
    # as astra (Sol 2x/1.5x, Luna 2x/1.5x). GPT-6 has no Terra tier.
    # gpt-6.1-sol (released 2026-09-29): Sol's price, but cached input is 0.05x
    # ($0.10), not 0.1x. Not a prefix of "gpt-6-sol", so without this row it fell
    # to the astra fallback at 5x. Same 272K caveat (2x input/cache, 1.5x output).
    "gpt-6.1-sol":       {"input": 2.00, "output": 10.00, "cache_read": 0.10,  "cache_write": 2.50},
    "gpt-6-sol":         {"input": 2.00, "output": 10.00, "cache_read": 0.20,  "cache_write": 2.50},
    "gpt-6-luna":        {"input": 0.10, "output":  0.50, "cache_read": 0.01,  "cache_write": 0.125},
    "gpt-5.6-sol":       {"input": 5.00, "output": 30.00, "cache_read": 0.50,  "cache_write": 6.25},
    "gpt-5.6-terra":     {"input": 2.50, "output": 15.00, "cache_read": 0.25,  "cache_write": 3.125},
    "gpt-5.6-luna":      {"input": 1.00, "output":  6.00, "cache_read": 0.10,  "cache_write": 1.25},
    "gpt-5.6":           {"input": 5.00, "output": 30.00, "cache_read": 0.50,  "cache_write": 6.25},  # bare id aliases to Sol
    "gpt-5.5":           {"input": 5.00, "output": 30.00, "cache_read": 0.50,  "cache_write": 0.0},
    "gpt-5.4-mini":      {"input": 0.75, "output":  4.50, "cache_read": 0.075, "cache_write": 0.0},
    "gpt-5.4":           {"input": 2.50, "output": 15.00, "cache_read": 0.25,  "cache_write": 0.0},
    "gpt-5.3-codex":     {"input": 1.75, "output": 14.00, "cache_read": 0.175, "cache_write": 0.0},
    "gpt-5.2-codex":     {"input": 1.75, "output": 14.00, "cache_read": 0.175, "cache_write": 0.0},
}


def _resolve_tiers(entry, timestamp=None):
    """Resolve a PRICING value (flat dict or ascending tier list) to the rate
    dict active on `timestamp`'s day. Plain ISO string comparison — timestamps
    are ISO-8601, no datetime parsing needed. `timestamp=None` means "now"
    (the current local date)."""
    if not isinstance(entry, list):
        return entry
    day = (timestamp or datetime.now().isoformat())[:10]
    chosen = entry[0]
    for tier in entry:
        if tier.get("from", "") <= day:
            chosen = tier
    return chosen


def get_pricing(model, timestamp=None):
    if not model:
        return None
    if model in PRICING:
        return _resolve_tiers(PRICING[model], timestamp)
    for key in PRICING:
        if model.startswith(key):
            return _resolve_tiers(PRICING[key], timestamp)
    # Substring fallback: match model family by keyword. Deliberately narrow —
    # non-priced providers (gpt-4o, glm-*, local models like qwen*) must stay
    # unmatched so they cost $0 rather than being mispriced.
    m = model.lower()
    if "fable-5-1" in m or "mythos-5-1" in m:
        return _resolve_tiers(PRICING["claude-fable-5-1"], timestamp)
    if "fable" in m or "mythos" in m:
        return _resolve_tiers(PRICING["claude-fable-5"], timestamp)
    # "opus-" (not bare "opus") so local builds that merely END in opus
    # (e.g. qwen3.6-40b-claude-46-opus) stay unmatched and cost $0, matching
    # isBillable. Real models and variants are "claude-opus-..."/"new-opus-5".
    if "opus-5-5" in m:
        return _resolve_tiers(PRICING["claude-opus-5-5"], timestamp)
    if "opus-" in m:
        return _resolve_tiers(PRICING["claude-opus-4-8"], timestamp)
    if "sonnet" in m:
        return _resolve_tiers(PRICING["claude-sonnet-4-6"], timestamp)
    if "haiku-5-5" in m:
        return _resolve_tiers(PRICING["claude-haiku-5-5"], timestamp)
    if "haiku" in m:
        return _resolve_tiers(PRICING["claude-haiku-4-5"], timestamp)
    if "codex" in m:
        return _resolve_tiers(PRICING["gpt-5.3-codex"], timestamp)
    if "gpt-5" in m:
        return _resolve_tiers(PRICING["gpt-5.4-mini"] if "mini" in m else PRICING["gpt-5.4"], timestamp)
    # An unknown gpt-6-* variant lands on astra (the top tier) rather than $0:
    # over-counting an unrecognized id beats silently pricing it free.
    if "gpt-6" in m:
        return _resolve_tiers(PRICING["gpt-6-astra"], timestamp)
    return None


def calc_cost(model, inp, out, cache_read, cache_creation, timestamp=None,
              cache_creation_1h=0):
    """Cost of one turn. ``cache_creation`` is the TOTAL cache-write tokens;
    ``cache_creation_1h`` is the portion of it written with a 1-hour TTL, which
    bills at 2x input instead of the 5-minute 1.25x. Defaulting it to 0 prices
    everything at the 5m rate — the correct answer for rows predating the split
    columns and for providers with no 1h tier."""
    p = get_pricing(model, timestamp)
    if not p:
        return 0.0
    # Clamp: the 1h portion can never exceed the total it is a part of.
    cache_1h = min(cache_creation_1h or 0, cache_creation or 0)
    return (
        inp                          * p["input"]       / 1_000_000 +
        out                          * p["output"]      / 1_000_000 +
        cache_read                   * p["cache_read"]  / 1_000_000 +
        (cache_creation - cache_1h)  * p["cache_write"] / 1_000_000 +
        cache_1h * p.get("cache_write_1h", p["cache_write"]) / 1_000_000
    )


# Per-turn price multiplier, applied to every token class: fast mode bills 2x
# (usage.speed == "fast") and US-only inference 1.1x (usage.inference_geo ==
# "us"); the two stack (platform.claude.com/docs/en/about-claude/pricing,
# 2026-09-24). Codex rows carry neither, so they stay at 1x. Cost is linear in
# tokens, so aggregates carry the premium as extra tokens, SUM(tokens * (mult - 1)),
# priced at the row's normal rates -- exact even with rates edited in the dashboard.
#
# Haiku 5.5 long prompts: every rate is 5x once a request's prompt exceeds 100K
# tokens (anthropic.com/claude-haiku-5-5, 2026-10-07). The announcement does not
# define "prompt"; we assume the Sonnet 4.5 long-context rule — prompt = input +
# cache_read + cache_creation, and the whole request (output too) bills at the
# higher rate. ponytail: unconfirmed until the platform pricing page defines it.
LONG_PROMPT_MODEL, LONG_PROMPT_TOKENS, LONG_PROMPT_MULT = "haiku-5-5", 100_000, 5.0
LONG_PROMPT_COLS = ("model", "input_tokens", "cache_read_tokens", "cache_creation_tokens")

SURCHARGE_COLS = (("input_tokens", "input"), ("output_tokens", "output"),
                  ("cache_read_tokens", "cache_read"),
                  ("cache_creation_tokens", "cache_creation"),
                  ("cache_creation_1h_tokens", "cache_creation_1h"))


def price_mult(speed, inference_geo, model="", prompt_tokens=0):
    """One turn's multiplier in Python (per-turn paths with no SQL); must agree
    with price_mult_sql. ``prompt_tokens`` = input + cache_read + cache_creation."""
    long_prompt = (LONG_PROMPT_MODEL in (model or "").lower()
                   and prompt_tokens > LONG_PROMPT_TOKENS)
    return ((2.0 if speed == "fast" else 1.0) * (1.1 if inference_geo == "us" else 1.0)
            * (LONG_PROMPT_MULT if long_prompt else 1.0))


def price_mult_sql(cols, t=""):
    """SQL for one turn's multiplier. ``cols`` is the turns table's column set: a
    DB that predates turns.speed still prices US-only rows (geo-only multiplier)
    rather than erroring. ``t`` is an optional table-alias prefix like 't.'."""
    fast = f"CASE WHEN {t}speed = 'fast' THEN 2.0 ELSE 1.0 END" if "speed" in cols else "1.0"
    geo = (f"CASE WHEN {t}inference_geo = 'us' THEN 1.1 ELSE 1.0 END"
           if "inference_geo" in cols else "1.0")
    # LIKE is case-insensitive for ASCII in SQLite, matching .lower() above.
    long_prompt = (f"CASE WHEN {t}model LIKE '%{LONG_PROMPT_MODEL}%' AND "
                   f"{t}input_tokens + {t}cache_read_tokens + {t}cache_creation_tokens"
                   f" > {LONG_PROMPT_TOKENS} THEN {LONG_PROMPT_MULT} ELSE 1.0 END"
                   if set(LONG_PROMPT_COLS) <= set(cols) else "1.0")
    return f"({fast} * {geo} * {long_prompt})"


def surcharge_sql(cols, t="", agg=True):
    """SELECT terms x_input/x_output/x_cache_read/x_cache_creation/x_cache_creation_1h:
    each token class's premium over standard pricing, as tokens. ``agg=False``
    gives per-turn values for un-grouped queries."""
    extra = price_mult_sql(cols, t) + " - 1"
    wrap = "SUM({})" if agg else "{}"
    return ", ".join(
        wrap.format(f"{t}{col} * ({extra})" if col in cols else "0") + f" AS x_{alias}"
        for col, alias in SURCHARGE_COLS)


def surcharge(row):
    """The ``x`` payload for a row selected with surcharge_sql: ``None`` when the
    row carries no premium (the common case), else a token dict the dashboard
    prices at the row's own rates (pricing.js rowCost)."""
    x = {alias: row[f"x_{alias}"] or 0 for _, alias in SURCHARGE_COLS}
    return x if any(x.values()) else None


def surcharge_cost(model, row, timestamp=None):
    """Dollar cost of a row's surcharge (see surcharge())."""
    x = surcharge(row)
    if not x:
        return 0.0
    return calc_cost(model, x["input"], x["output"], x["cache_read"], x["cache_creation"],
                     timestamp, cache_creation_1h=x["cache_creation_1h"])
