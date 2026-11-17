"""Model what-if: the cost of the same tokens on another model. Observation only.

Reprices turns through pricing.reprice_turn (calc_cost + the fast-mode / US-only /
long-prompt multipliers). The figures never enter attributed overhead, the waste totals
or Est. Cost; nothing here is imported by waste.py or the rollups.
"""

import api
import pricing

WORDING = "Repricing only. It does not say the cheaper model would have done the work."
NEXT_TIER = "next_tier"  # `to` value: each model repriced onto its own next tier down


def reprice(to, start="", end="", model="", project="", actor="", session_id="", tz="", db_path=None):
    """{to, actual_usd, repriced_usd, delta_usd, by_model, wording} for the turns in scope.

    ``to`` is a model id (must be priced) or NEXT_TIER. ``actor``: "main", "subagent" or an
    agent type, resolved as in api.get_session_breakdown (agents.agent_type, else main).
    Models with no next tier down keep ``to`` None and are left out of the repriced total.
    ``start``/``end`` are UTC days unless ``tz="local"``, which buckets days the way the dashboard does."""
    db_path = db_path or api.DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}
    if to != NEXT_TIER and not pricing.get_pricing(to):
        return {"error": f"unknown model: {to!r}"}
    conn = api._connect(db_path)
    try:
        cols = api._turn_cols(conn)
        clauses, params, join = [], [], ""
        day = ("COALESCE(date(t.timestamp, 'localtime'), substr(t.timestamp, 1, 10))" if tz == "local"
               else "substr(t.timestamp, 1, 10)")
        if start:
            clauses.append(f"{day} >= ?"); params.append(start)
        if end:
            clauses.append(f"{day} <= ?"); params.append(end)
        if model:
            clauses.append("COALESCE(NULLIF(t.model, ''), 'unknown') = ?"); params.append(model)
        if session_id:
            clauses.append("t.session_id = ?"); params.append(session_id)
        if project:
            join += " JOIN sessions s ON s.session_id = t.session_id"
            clauses.append("s.project_name = ?"); params.append(project)
        if actor:
            if "agent_id" in cols:
                join += " LEFT JOIN agents a ON a.agent_id = t.agent_id"
                side = "COALESCE(t.is_sidechain, 0)" if "is_sidechain" in cols else "0"  # a lane with no typed agent row is still a subagent
                if actor == "main":
                    clauses.append(f"(a.agent_type IS NULL AND {side} = 0)")
                elif actor == "subagent":
                    clauses.append(f"(a.agent_type IS NOT NULL OR {side} = 1)")
                else:
                    clauses.append("a.agent_type = ?"); params.append(actor)
            elif actor != "main":
                clauses.append("0")
        sel = lambda c, d="NULL": f"t.{c}" if c in cols else d
        rows = conn.execute(f"""
            SELECT COALESCE(NULLIF(t.model, ''), 'unknown') AS model, t.timestamp AS timestamp,
                   t.input_tokens AS input, t.output_tokens AS output,
                   t.cache_read_tokens AS cache_read, t.cache_creation_tokens AS cache_creation,
                   {sel('cache_creation_1h_tokens', '0')} AS cache_creation_1h,
                   {sel('speed')} AS speed, {sel('inference_geo')} AS inference_geo
            FROM turns t {join}
            {('WHERE ' + ' AND '.join(clauses)) if clauses else ''}
        """, params).fetchall()
    finally:
        conn.close()

    by_model = {}
    for r in rows:
        r = dict(r)
        target = pricing.next_tier_down(r["model"]) if to == NEXT_TIER else to
        m = by_model.setdefault(r["model"], {"model": r["model"], "to": target, "turns": 0,
                                             "actual_usd": 0.0, "repriced_usd": 0.0})
        m["turns"] += 1
        m["actual_usd"] += pricing.reprice_turn(r, r["model"])
        if target:
            m["repriced_usd"] += pricing.reprice_turn(r, target)
    out = []
    for m in sorted(by_model.values(), key=lambda m: -m["actual_usd"]):
        m["delta_usd"] = round(m["repriced_usd"] - m["actual_usd"], 6) if m["to"] else None
        m["actual_usd"], m["repriced_usd"] = round(m["actual_usd"], 6), round(m["repriced_usd"], 6)
        out.append(m)
    priced = [m for m in out if m["to"]]
    actual = round(sum(m["actual_usd"] for m in priced), 6)
    repriced = round(sum(m["repriced_usd"] for m in priced), 6)
    return {"to": to, "actual_usd": actual, "repriced_usd": repriced,
            "delta_usd": round(repriced - actual, 6), "by_model": out, "wording": WORDING}
