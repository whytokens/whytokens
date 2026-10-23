"""Session cost-waste attribution built from the read-only ``turns`` table.

``resident_output`` and ``repeated_reads`` charge only the next turn's
cache-write, not cache-reads carried by later turns. Their dollars therefore
under-report absolute cost and are useful for ranking, not totals.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pricing


CAUSES = ("resident_output", "repeated_reads", "subagent_preamble", "idle_gap", "uncached_input")
# Evidence tiers (plan decision 4). Nothing here is measured before/after, so no cause may
# claim "demonstrated saving" until v1.1.
TIERS = {"resident_output": "attributed cause", "repeated_reads": "attributed cause",
         "subagent_preamble": "attributed cause", "idle_gap": "attributed cause",
         "uncached_input": "observation"}
# One pricing basis for findings and totals: pricing.calc_cost at API list rate.
BASIS = {"pricing": "API list rate",
         "label": "attributed, lower bound, at API list rate; not savings"}
# Sources a detector is validated for. Anything else is shown "not measurable", never silent.
_MEASURED = {"claude"}
_WHY = {"codex": "Codex reports cached input but no cache-write tokens, tool-call file paths or "
                 "sidechain turns, so this detector has no signal"}
_WHY_DEFAULT = "no detector is validated for this source"


def measurement(db_path):
    """Per cause x source: measured, or not measurable with the reason. Sources are those in the DB."""
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        sources = sorted({r[0] or "claude" for r in conn.execute("SELECT DISTINCT agent FROM turns")})
    finally:
        conn.close()
    cell = lambda src: ({"status": "measured", "reason": ""} if src in _MEASURED else
                        {"status": "not measurable", "reason": _WHY.get(src, _WHY_DEFAULT)})
    return {"sources": sources, "causes": {c: {s: cell(s) for s in sources} for c in CAUSES}}


def _calls(row):
    try:
        calls = json.loads(row["tool_calls"] or "[]")
    except (TypeError, ValueError):
        return []
    return calls if isinstance(calls, list) else []


def _tool_info(row, seen_paths):
    calls = _calls(row)
    repeated = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get("name") != "Read":
            continue
        path = (call.get("input") or {}).get("file_path")
        if path in seen_paths:
            repeated.append(path)
        if path:
            seen_paths.add(path)
    return bool(calls or row["tool_name"]), repeated


def _cost(row, inp=0, creation=0):
    return pricing.calc_cost(row["model"], inp, 0, 0, creation,
                             row["timestamp"], row["cache_creation_1h_tokens"] if creation else 0) * row["mult"]


def _claim(claims, cause, turn, tokens, detail, tool, inp=False):
    usd = _cost(turn, inp=tokens) if inp else _cost(turn, creation=tokens)
    claims[cause][0] += usd
    claims[cause][1].append(detail)
    claims[cause][2].append({"at": turn["timestamp"], "tokens": tokens, "usd": usd, "tool": tool})


def _gap(a, b):
    try:
        return datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def report(db_path, session_id=None):
    """Return thresholded, disjoint cost findings for one session or a database."""
    db_path = Path(db_path).resolve().as_posix()
    # quote: an unencoded '#' or '?' in the path would silently open a different database
    conn = sqlite3.connect(f"file:{quote(str(db_path), safe='/:')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = """SELECT id, session_id, timestamp, model, input_tokens, output_tokens,
                         cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                         tool_name, tool_calls, is_sidechain, is_compact_summary, agent,
                         {mult} AS mult
                  FROM turns""".format(mult=pricing.price_mult_sql(
                      {r[1] for r in conn.execute("PRAGMA table_info(turns)")}))
        rows = conn.execute(sql + (" WHERE session_id=?" if session_id else "") +
                            " ORDER BY session_id, timestamp, id", (session_id,) if session_id else ()).fetchall()
    finally:
        conn.close()

    sessions = {}
    for row in rows:
        if (row["agent"] or "claude") not in _MEASURED:
            continue  # unmeasurable sources attribute nothing; measurement() says so
        sessions.setdefault(row["session_id"], []).append(row)
    findings = []
    for sid, turns in sessions.items():
        claims = {cause: [0.0, [], []] for cause in CAUSES}
        seen_paths, previous_main, previous_turn = set(), None, None
        for turn in turns:
            creation = turn["cache_creation_tokens"] or 0
            if turn["is_sidechain"]:
                if creation:
                    _claim(claims, "subagent_preamble", turn, creation, creation, "Agent")
                previous_turn = turn
                continue
            compact = turn["is_compact_summary"]
            has_tool, repeated = _tool_info(turn, seen_paths) if not compact else (False, [])
            gap = _gap(previous_turn["timestamp"], turn["timestamp"]) if previous_turn else None
            if creation and previous_main:
                if gap and gap.total_seconds() >= 1800:
                    _claim(claims, "idle_gap", turn, creation, (gap, creation), None)
                elif not compact and previous_main["repeated"]:
                    _claim(claims, "repeated_reads", turn, creation, (previous_main["repeated"], creation), "Read")
                elif not compact and previous_main["has_tool"]:
                    _claim(claims, "resident_output", turn, creation,
                           (previous_main["tool_name"] or "tool", creation), previous_main["tool_name"])
            if ((turn["agent"] or "claude") == "claude" and not (turn["cache_read_tokens"] or 0)
                    and not creation and (turn["input_tokens"] or 0)):
                _claim(claims, "uncached_input", turn, turn["input_tokens"], turn["input_tokens"], None, inp=True)
            previous_main = {"timestamp": turn["timestamp"], "has_tool": has_tool,
                             "repeated": repeated, "tool_name": turn["tool_name"]}
            previous_turn = turn

        total = sum(pricing.calc_cost(t["model"], t["input_tokens"] or 0, t["output_tokens"] or 0,
                                      t["cache_read_tokens"] or 0, t["cache_creation_tokens"] or 0,
                                      t["timestamp"], t["cache_creation_1h_tokens"] or 0) * t["mult"]
                    for t in turns)
        floor = max(.01, total * .05)
        for cause, (usd, details, evidence) in claims.items():
            if usd < floor:
                continue
            if cause == "resident_output":
                ranked = sorted(((sum(n for other, n in details if other == name), name)
                                 for name in {name for name, _ in details}), reverse=True)
                names = ", ".join(f"{name} ({tokens:,})" for tokens, name in ranked[:3])
                if len(ranked) > 3:
                    names += f" (+{len(ranked) - 3} more)"
                detail, fix = (f"{names} caused {sum(n for _, n in details):,} cache-write tokens of context growth.",
                               "Trim or summarize tool output before it returns to the conversation.")
            elif cause == "repeated_reads":
                # basenames only: findings are shown and shared, full paths carry usernames
                files = sorted({re.split(r"[\\/]", str(path))[-1] for paths, _ in details for path in paths})
                paths = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
                detail, fix = (f"Repeated Read calls of {paths} added {sum(n for _, n in details):,} cache-write tokens.",
                               "Read the file once, then keep or summarize the needed excerpt.")
            elif cause == "subagent_preamble":
                detail, fix = (f"Sidechain turns wrote {sum(details):,} cache-create tokens for inherited preambles.",
                               "Give subagents narrower prompts and reuse fewer independent agents.")
            elif cause == "idle_gap":
                minutes = max((g.total_seconds() / 60 for g, _ in details), default=0)
                detail, fix = (f"{len(details)} idle gaps (longest {minutes:.0f} min) rewrote {sum(n for _, n in details):,} cache tokens.",
                               ("Start a fresh session instead of resuming a stale one; re-onboard from the handoff file."
                                if minutes > 240 else
                                "Resume before the cache expires, or compact context before a long pause."))
            else:
                detail, fix = (f"{sum(details):,} input tokens were billed without a cache read or write.",
                               "Keep repeated prompt material in a cacheable prefix.")
            top = sorted(evidence, key=lambda e: e["usd"], reverse=True)[:5]
            findings.append({"cause": cause, "session_id": sid, "usd": usd, "detail": detail, "fix": fix,
                             "tier": TIERS[cause], "advice": f"{TIERS[cause].capitalize()}: {fix}",
                             "evidence": top, "session_usd": total})
    return sorted(findings, key=lambda finding: finding["usd"], reverse=True)
