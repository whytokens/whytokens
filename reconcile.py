"""Reconcile whytokens' month figure with the bill: the gap split into named, computed causes.

Design, and the order that keeps the rows disjoint: docs/private/reconcile-spec.md. The billed
figure the user types lives in budget.json beside the monthly budget,
``"billed": {"YYYY-MM": {"usd": 285.63, "as_of": "2026-11-20T15:00:00Z"}}``, written through
budget.py's lock. ``as_of`` is when the bill was read; null for a closed month.
"""

import calendar
import functools
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import api
import budget
import config
import cost_state
import pricing
import scanner
from cli import LOCAL_DAY, cost_layers, dated_costs

PERIOD_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])", re.ASCII)
NOT_CODEX = "COALESCE(agent, 'claude') != 'codex'"
SUMMARY = f"COALESCE(message_id, '') LIKE '{scanner.AGENT_SUMMARY_ID_PREFIX}%'"
ADVISOR_EST = f"COALESCE(message_id, '') LIKE '{scanner.ADVISOR_ID_PREFIX}%:est'"
# Request-level rows only: summaries, advisor estimates and coordinator rows aggregate or copy
# prompt sizes, so the per-request 100K line can't be judged on them.
LONG_PROMPT = (f"model LIKE '%{pricing.LONG_PROMPT_MODEL}%' AND input_tokens + cache_read_tokens"
               f" + cache_creation_tokens > {pricing.LONG_PROMPT_TOKENS} AND NOT {SUMMARY}"
               f" AND NOT {ADVISOR_EST} AND COALESCE(agent, 'claude') = 'claude'")


def bounds(period):
    """``YYYY-MM`` -> (first day, last day, UTC start, UTC end) of the calendar month cut at 00:00 UTC."""
    m = PERIOD_RE.fullmatch(period) if isinstance(period, str) else None
    if not m:
        raise ValueError("period must be YYYY-MM")
    y, mo = int(m[1]), int(m[2])
    first, last = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    return first, last, first.isoformat() + "T00:00:00Z", (last + timedelta(days=1)).isoformat() + "T00:00:00Z"


def _amount(v):
    """A billed amount: finite and >= 0 ($0.00 is a real reading). ValueError otherwise."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError("amount must be a finite number of 0 or more")
    return float(v)


def _instant(s):
    """An ISO date-time -> 'YYYY-MM-DDTHH:MM:SS.ffffffZ' in UTC (naive means UTC), fractional
    seconds kept. ValueError otherwise, including a bare date."""
    if not isinstance(s, str) or "T" not in s.upper():
        raise ValueError("as_of must be an ISO date-time")
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _not_future(first, today=None):
    """A month that hasn't started anywhere yet is an error. UTC decides, so the first hours of a
    UTC month are valid even where the local calendar is still in the previous one."""
    if first > max(today or date.min, datetime.now(timezone.utc).date()):
        raise ValueError("period is in the future")


def set_billed(period, usd, as_of=None, db_path=None):
    """Store the billed figure for ``period`` read at ``as_of`` (None = the month is closed);
    ``usd`` None clears it. Other settings in budget.json are kept."""
    _not_future(bounds(period)[0])
    entry = None if usd is None else {"usd": _amount(usd), "as_of": None if as_of is None else _instant(as_of)}
    path = budget.budget_path(db_path)
    with budget._locked(path):
        d = budget._read(path, strict=True)
        billed = d.get("billed") if isinstance(d.get("billed"), dict) else {}
        if entry is None:
            billed.pop(period, None)
        else:
            billed[period] = entry
        if billed:
            d["billed"] = billed
        else:
            d.pop("billed", None)
        budget._write(path, d)
    return entry


def load_billed(period, db_path=None):
    e = budget._read(budget.budget_path(db_path)).get("billed")
    e = e.get(period) if isinstance(e, dict) else None
    try:
        return {"usd": _amount(e["usd"]), "as_of": e.get("as_of") and _instant(e["as_of"])}
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


@functools.lru_cache(maxsize=1 << 18)  # each stored timestamp is normalized once per process, not once per query
def _ts(s):
    """A stored timestamp as a sortable UTC microsecond string (NULL if unparseable); SQLite's
    julianday() rounds below a millisecond, which flips a strict cutoff."""
    try:
        return _instant(s.replace(" ", "T", 1) if isinstance(s, str) and "T" not in s.upper() else s)
    except (TypeError, ValueError, OverflowError):
        return None


def _cost(conn, where, params):
    """(dollars, stored rows) for the turns matching ``where``, priced per UTC day like every figure,
    web-search fees included."""
    dollars = cost_layers(conn, where, params)["total"]
    return dollars, conn.execute(f"SELECT COUNT(*) FROM turns WHERE {where}", params).fetchone()[0]


def _token_cost(conn, where, params):
    """_cost without the web-search fee: the 5x premium scales tokens, never the fee."""
    return sum(dated_costs(conn, where, params).values()), conn.execute(
        f"SELECT COUNT(*) FROM turns WHERE {where}", params).fetchone()[0]


def report(period, db_path=None, today=None, tz="local"):
    """The reconcile for one UTC calendar month. ``tz`` is the cut the dashboard shows its month
    in ('local' or 'utc'). With no billed figure stored, gap and remainder are None."""
    db_path = Path(db_path or config.DB_PATH)
    first, last, start, end = bounds(period)
    today = today or (datetime.now(timezone.utc).date() if tz == "utc" else date.today())
    _not_future(first, today)
    bill = load_billed(period, db_path)
    as_of = bill and bill["as_of"]
    # The reading's moment cuts both figures, whichever side of either month boundary it falls.
    upto = " AND wt_ts(timestamp) < ?" if as_of else ""
    tail = (as_of,) if as_of else ()
    day = "substr(timestamp, 1, 10)" if tz == "utc" else LOCAL_DAY
    shown_where = f"{day} >= ? AND {day} <= ?{upto}"
    shown_p = (first.isoformat(), last.isoformat()) + tail
    utc = f"wt_ts(timestamp) >= ? AND wt_ts(timestamp) < ?{upto}"
    p = (_instant(start), _instant(end)) + tail

    conn = api._connect(db_path)
    conn.create_function("wt_ts", 1, _ts, deterministic=True)
    try:
        conn.execute("BEGIN")  # one read snapshot: a scan landing mid-report can't skew one row
        shown = _cost(conn, shown_where, shown_p)
        utc_all = _cost(conn, utc, p)
        codex = _cost(conn, f"{utc} AND NOT ({NOT_CODEX})", p)
        summaries = _cost(conn, f"{utc} AND {NOT_CODEX} AND {SUMMARY}", p)
        long_full = _token_cost(conn, f"{utc} AND {LONG_PROMPT}", p)
        advisor = _cost(conn, f"{utc} AND {NOT_CODEX} AND {ADVISOR_EST}", p)
        coordinator = _cost(conn, f"{utc} AND agent = 'coordinator'", p)
        raised = conn.execute(
            f"SELECT COUNT(*) FROM turns WHERE {utc} AND {NOT_CODEX} AND estimated = 1"
            f" AND NOT {SUMMARY} AND NOT {ADVISOR_EST} AND COALESCE(agent, 'claude') = 'claude'", p).fetchone()[0]
        claude = "COALESCE(agent, 'claude') = 'claude'"
        first_ts = f"SELECT timestamp FROM turns WHERE {claude}{{}} ORDER BY julianday(timestamp) LIMIT 1"
        earliest = (conn.execute(first_ts.format("")).fetchone() or [None])[0]
        in_period = (conn.execute(first_ts.format(f" AND {utc}"), p).fetchone() or [None])[0]
        new_projects = [dict(project=r[0], first_seen=r[1]) for r in conn.execute(
            "SELECT COALESCE(s.project_name, '?'), substr(MIN(wt_ts(t.timestamp)), 1, 10) AS f, MIN(wt_ts(t.timestamp)) AS j"
            " FROM turns t JOIN sessions s ON s.session_id = t.session_id"
            " WHERE COALESCE(t.agent, 'claude') = 'claude' GROUP BY 1"
            f" HAVING j >= ? AND j < ?{' AND j < ?' if as_of else ''}"
            " ORDER BY j", p)]
    finally:
        conn.close()

    # Every multiplier scales all token classes alike, so the row's cost without the 5x tier
    # is cost / 5 and the premium is the other 4/5 (pricing.price_mult).
    premium = long_full[0] * (1 - 1 / pricing.LONG_PROMPT_MULT)
    before = (0 if earliest and earliest[:10] < first.isoformat() else
              (min(date.fromisoformat(earliest[:10]), last + timedelta(days=1)) - first).days if earliest
              else (min(today, last) - first).days + 1)
    rows = [
        dict(key="day_boundary", kind="measured", turns=None, usd=shown[0] - utc_all[0],
             label=f"Day boundary ({'UTC' if tz == 'utc' else 'local'} days shown vs the UTC month)"),
        dict(key="other_vendor", kind="measured", usd=codex[0], turns=codex[1],
             label="Other vendors (Codex), not on a Claude bill"),
        dict(key="estimated", kind="scenario", usd=summaries[0], turns=summaries[1],
             label="If subagent summaries aren't billed: estimated rows, by stored row date"),
        dict(key="haiku_5x", kind="scenario", usd=premium, turns=long_full[1],
             label=f"If the Haiku 5.5 long-prompt 5x tier isn't applied: premium "
                   f"(${long_full[0]:,.2f} with, ${long_full[0] - premium:,.2f} without)"),
    ]
    gap = None if bill is None else shown[0] - bill["usd"]
    remainder = None if gap is None else gap - sum(r["usd"] for r in rows)
    r6 = lambda v: None if v is None else round(v, 6)
    for r in rows:
        r["usd"] = r6(r["usd"])
    return {
        "period": period, "cut": "calendar month, 00:00 UTC", "tz": tz, "as_of": as_of,
        "billed": bill and bill["usd"], "shown": r6(shown[0]), "utc_total": r6(utc_all[0]), "gap": r6(gap),
        "rows": rows,
        "remainder": dict(key="remainder", usd=r6(remainder),
                          label="Unexplained: no local transcript (claude.ai, cloud, other devices), "
                                "contract rates, deleted transcripts"),
        # Deleted transcripts can't be priced; what the DB can say is where its data starts.
        "coverage": dict(earliest_claude=earliest, first_in_period=in_period, days_before_earliest=before,
                         projects_first_seen_in_period=new_projects,
                         label="completeness unknown: transcripts deleted before the first scan leave no trace"
                               + (f"; no Claude data for the first {before} day(s) of the month" if before else "")),
        "info": {
            "advisor_estimates": dict(usd=r6(advisor[0]), turns=advisor[1]),
            "raised_output_turns": raised,
            "coordinator_lower_bound": dict(usd=r6(coordinator[0]), turns=coordinator[1]),
        },
        "basis": "estimated at API list price",
    }


def session_check(session_id, usage=None, db_path=None):
    """One session's whytokens cost next to the /usage figure read for it (not stored: a session
    still running grows, so each check is a one-off). ``last_turn`` shows how fresh the scan is.
    With no ``usage`` given, Claude Code's own cost-state total fills it when that record covers
    the whole session (``usage_source``: "cost-state"); an entered figure always wins."""
    if not session_id:
        raise ValueError("session id required")
    usage = None if usage is None else _amount(usage)
    db_path = Path(db_path or config.DB_PATH)
    conn = api._connect(db_path)
    try:
        conn.execute("BEGIN")
        total = _cost(conn, "session_id = ?", (session_id,))
        layers = cost_layers(conn, "session_id = ?", (session_id,))
        est = _cost(conn, f"session_id = ? AND {SUMMARY}", (session_id,))
        long_full = _token_cost(conn, f"session_id = ? AND {LONG_PROMPT}", (session_id,))
        last_turn = (conn.execute("SELECT timestamp FROM turns WHERE session_id = ?"
                                  " ORDER BY julianday(timestamp) DESC LIMIT 1", (session_id,)).fetchone() or [None])[0]
        cc = cost_state.compare(conn, session_id).get(session_id) or {}
    finally:
        conn.close()
    if not total[1]:
        raise ValueError(f"no turns for session {session_id} (scan first?)")
    source = "entered" if usage is not None else None
    if usage is None and cc.get("cc_coverage") == "full":
        usage, source = cc["cc_cost"], "cost-state"
    return {
        "session_id": session_id, "whytokens": round(total[0], 6), "turns": total[1], "last_turn": last_turn,
        "estimated": round(est[0], 6),
        "layers": {k: (v if k == "web_searches" else round(v, 6)) for k, v in layers.items()},
        "haiku_5x": round(long_full[0] * (1 - 1 / pricing.LONG_PROMPT_MULT), 6),
        "usage": usage, "delta": None if usage is None else round(total[0] - usage, 6),
        "usage_source": source, "cc_cost": cc.get("cc_cost"), "cc_coverage": cc.get("cc_coverage", "none"),
        "cc_flag": cc.get("cc_flag", False), "models": cc.get("models", []),
    }


def current_period():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def default_as_of(period):
    """A bill read during its own month is month-to-date: cut whytokens at the moment of reading."""
    return _instant(datetime.now(timezone.utc).isoformat()) if period == current_period() else None
