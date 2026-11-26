"""Claude Code's own ``cost-state`` record as a per-session checksum (docs/SCHEMA.md ``cost_state``).

Ingest (sources/claude.py) stores the last record per session; everything here is computed at
read time and never stored, so a pricing change re-labels old sessions on the next read.
"""

import json
from datetime import datetime, timezone

import pricing
from cli import _surcharge, row_cost

BACKFILL_MARKER = "cost_state_backfill_done_claude"
SLACK_MS = 60_000      # the process starts a little before its first API call
REPRICE_TOL = 0.01     # own reprice vs the record's costUSD
FLAG_PCT = 0.05        # |whytokens - Claude Code| / Claude Code
FLAG_MIN_USD = 0.10    # below this a few cents of side calls swing the ratio


def norm(model):
    return (model or "").split("[")[0]  # "claude-opus-5-5[1m]" is the same model and rates


def covers_full(start_ms, first_turn_ms):
    """1 if the record saw the whole session (its counters started by the first turn), 0 if a later
    process restarted them, None with no turn to compare against (not shown at all)."""
    if first_turn_ms is None:
        return None
    if start_ms is None:
        return 0  # an unreadable start: never compared
    return int(start_ms <= first_turn_ms + SLACK_MS)


def num(v):
    """A finite number from a record field, else 0: a malformed record must not break a read."""
    # a range check, not math.isfinite: that raises OverflowError on a huge JSON int (10**400)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and -1e18 < v < 1e18 else 0


def label(model, u, day):
    """'clean' | 'priced_differently' | 'unverifiable' for one modelUsage entry."""
    n = lambda k: num(u.get(k))
    if pricing.LONG_PROMPT_MODEL in model and n("cacheReadInputTokens"):
        return "unverifiable"  # the 5x tier is per request; an aggregate can't tell
    if n("cacheCreationInputTokens") and "cacheCreationEphemeral1hInputTokens" not in u:
        return "unverifiable"  # older records carry no 5m/1h split, and the two rates differ
    # a Foundry -fast id bills at the shared premium (pricing.price_mult); web-search fees are not scaled
    re = (pricing.price_mult("", "", model)
          * pricing.calc_cost(model, n("inputTokens"), n("outputTokens"), n("cacheReadInputTokens"),
                              n("cacheCreationInputTokens"), day,
                              cache_creation_1h=n("cacheCreationEphemeral1hInputTokens"))
          + n("webSearchRequests") * pricing.WEB_SEARCH_FEE_USD)
    # the 1e-9 keeps float noise at exactly the tolerance on the clean side
    return "priced_differently" if abs(re - n("costUSD")) > REPRICE_TOL * n("costUSD") + 1e-9 else "clean"


def _ids(session_ids):
    """(sql, params) restricting to the given ids, or to every recorded session for None."""
    if session_ids is None:
        return "session_id IN (SELECT session_id FROM cost_state)", ()
    return f"session_id IN ({','.join('?' * len(session_ids))})", tuple(session_ids)


def _wt_costs(conn, session_ids):
    """{session_id: {normalised model: whytokens cost}} for sessions with a record."""
    where, params = _ids(session_ids)
    out = {}
    for r in conn.execute(f"""
            SELECT session_id, COALESCE(model, 'unknown') as model, substr(timestamp, 1, 10) as day,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   SUM(cache_creation_1h_tokens) as cc1h, {_surcharge(conn)}
            FROM turns WHERE {where} GROUP BY session_id, model, day""", params):
        m = out.setdefault(r["session_id"], {})
        m[norm(r["model"])] = m.get(norm(r["model"]), 0.0) + row_cost(r, r["day"])
    return out


def has_table(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cost_state'").fetchone())


def compare(conn, session_ids=None):
    """{session_id: comparison} for the given session id(s), or every session with a record
    (None). A DB that predates the table returns {}."""
    if isinstance(session_ids, str):
        session_ids = [session_ids]
    if session_ids == [] or not has_table(conn):
        return {}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")  # one snapshot: a scan committing between the two reads can't skew a gap
    try:
        where, params = _ids(session_ids)
        rows = conn.execute(f"SELECT * FROM cost_state WHERE {where}", params).fetchall()
        wt_all = _wt_costs(conn, session_ids) if rows else {}
    finally:
        if own:
            conn.execute("ROLLBACK")  # SQL, so it also ends the transaction under autocommit=True
    out = {}
    for r in rows:
        if r["covers_full"] is None:
            continue  # no turns: nothing of whytokens' to set it against
        cs = float(num(r["total_cost_usd"]))
        res = {"cc_cost": round(cs, 6), "cc_coverage": "full" if r["covers_full"] else "partial",
               "cc_delta_pct": None, "cc_flag": False, "models": []}
        out[r["session_id"]] = res
        if not r["covers_full"]:
            continue  # a later process reset the counters: stored, never compared
        wt_models = wt_all.get(r["session_id"], {})
        wt = sum(wt_models.values())
        try:
            day = datetime.fromtimestamp(num(r["start_time_ms"]) / 1000, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            day = None
        cs_models = {}
        try:
            usage = json.loads(r["model_usage"] or "{}")
        except ValueError:
            usage = {}
        for model, u in (usage.items() if isinstance(usage, dict) else ()):
            if not isinstance(u, dict):
                continue
            m = cs_models.setdefault(norm(model), {"cost": 0.0, "labels": set()})
            m["cost"] += num(u.get("costUSD"))
            m["labels"].add(label(norm(model), u, day))
        for model in sorted(set(cs_models) | set(wt_models)):
            labels = cs_models.get(model, {}).get("labels", set())
            res["models"].append({
                "model": model, "cc_cost": round(cs_models.get(model, {}).get("cost", 0.0), 6),
                "wt_cost": round(wt_models.get(model, 0.0), 6),
                "label": next((l for l in ("priced_differently", "unverifiable") if l in labels),
                              "clean" if labels else "not_in_record"),
            })
        delta = (wt - cs) / cs if cs > 0 else None
        res["cc_delta_pct"] = None if delta is None else round(delta, 6)  # rounded for display only
        res["cc_flag"] = bool(cs >= FLAG_MIN_USD and delta is not None
                              and abs(delta) > FLAG_PCT * (1 + 1e-12)  # float noise at exactly 5% is not over it
                              and not any(m["label"] == "priced_differently" for m in res["models"]))
    return out
