"""
api.py - Local web dashboard server (stdlib http.server).

Serves the frontend from ``static/`` (real HTML/CSS/ES-module files, no build, no
CDN) and the JSON read-API the frontend calls. The SQLite schema is the contract;
these endpoints only read it (except POST /api/rescan, which rebuilds the DB).

Routes:
  GET  /  , /index.html        -> static/index.html
  GET  /static/<path>          -> files under static/ (path-traversal guarded)
  GET  /api/data?tz            -> get_dashboard_data()  (full history; client filters.
                                  tz=utc buckets every `day` field by UTC calendar
                                  day instead of the server machine's local day)
  GET  /api/session?session_id -> get_session_detail()
  GET  /api/session-breakdown?session_id -> get_session_breakdown()
  GET  /api/sessions?project&model&start&end&limit&offset&sort&tz
                               -> get_sessions_page()  (SQL-side filter + pagination)
  GET  /api/daily?start&end&model&project&tz
                               -> get_daily()  (SQL-side aggregation + server cost;
                                  tz=local buckets `day` by server-local calendar day)
  GET  /api/extensions         -> tabs contributed by loaded extensions (docs/EXTENSIONS.md)
  GET  /ext/<name>/<path>      -> an extension's static files (containment-checked)
  GET  <extension routes>      -> consulted after the core routes above
  GET  /api/live?session_id|cwd -> get_live_session()  (transcript-direct, no DB)
  GET  /api/waste              -> waste findings, rollup, measurement status (API list rate)
  GET  /api/pricing            -> the single-source pricing table (pricing.PRICING)
  GET  /icon.svg               -> resources/icon.svg (header icon)
  POST /api/rescan             -> incremental sources.scan_all() (non-destructive)
"""

import collections
import json
import os
import secrets
import re
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import config
import extensions
import pricing
import scanner
import scanrun
from config import DB_PATH, DEFAULT_PORT


def _day_expr(tz, col="timestamp"):
    """SQL for the calendar day of an ISO-8601-UTC column, in the display timezone.

    `tz="utc"` slices the stored string; anything else uses SQLite's `localtime`
    modifier (per-timestamp, so DST-aware where a fixed offset would not be). The
    substr fallback covers a malformed/unparseable timestamp, as does _day_of."""
    if tz == "utc":
        return f"substr({col}, 1, 10)"
    return f"COALESCE(date({col}, 'localtime'), substr({col}, 1, 10))"


def _day_of(iso_utc_ts, tz="local"):
    """_day_expr's bucketing applied to a timestamp already read out of the DB, so
    the day fields the client range-filters on (`last_date`) land in the same
    calendar as the SQL buckets. Falls back to the raw UTC slice, as the COALESCE
    does."""
    if tz == "utc":
        return (iso_utc_ts or "")[:10]
    try:
        return datetime.fromisoformat(iso_utc_ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except Exception:
        return (iso_utc_ts or "")[:10]


# The frontend lives next to this file in static/. STATIC_ROOT is resolved once so
# the /static handler can confirm every requested path stays inside it.
STATIC_ROOT = (Path(__file__).resolve().parent / "static")

# Content types for static assets. The frontend is plain HTML/CSS/ES-modules plus
# the vendored Chart.js; .map covers source maps if a vendor lib ships one.
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "text/javascript; charset=utf-8",
    ".mjs":  "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg":  "image/svg+xml",
    ".map":  "application/json",
    ".ico":  "image/x-icon",
    ".png":  "image/png",
    ".woff2": "font/woff2",
}


def _qs1(query_dict, key, default=""):
    """First value for a parse_qs key, or default. parse_qs maps each key to a
    list; the read endpoints only ever want the first occurrence."""
    return query_dict.get(key, [default])[0]


def _connect(db_path):
    """Open a read connection with row access + a short busy timeout.

    The DB runs in WAL mode (set by scanner.get_db), so reads never block a
    concurrent scan; busy_timeout only covers the rare writer/writer overlap and
    keeps an external reader from erroring if it happens to open mid-commit.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _turn_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(turns)")}


def _session_model_breakdowns(conn, session_ids=None):
    """Return ``{session_id: [{model, input, output, cache_read, cache_creation}]}``
    — each session's tokens split by the model that actually produced them.

    Cost must be computed per model and summed (pricing.py): applying one label to a
    session's summed tokens misprices any session that touched more than one model
    (a subagent on a different model, or a mid-session /model switch). When
    ``session_ids`` is given the scan is restricted to that page of sessions.
    """
    sql = """
        SELECT session_id,
               COALESCE(NULLIF(model, ''), 'unknown') AS model,
               SUM(input_tokens)          AS input,
               SUM(output_tokens)         AS output,
               SUM(cache_read_tokens)     AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation,
               SUM(cache_creation_1h_tokens)  AS cache_creation_1h,
               {x}
        FROM turns
    """.format(x=pricing.surcharge_sql(_turn_cols(conn)))
    params = []
    if session_ids is not None:
        ids = list(session_ids)
        if not ids:
            return {}
        sql += " WHERE session_id IN (%s)" % ",".join("?" * len(ids))
        params = ids
    sql += " GROUP BY session_id, COALESCE(NULLIF(model, ''), 'unknown')"

    out = {}
    for r in conn.execute(sql, params):
        out.setdefault(r["session_id"], []).append({
            "model":          r["model"],
            "input":          r["input"] or 0,
            "output":         r["output"] or 0,
            "cache_read":     r["cache_read"] or 0,
            "cache_creation": r["cache_creation"] or 0,
            "cache_creation_1h": r["cache_creation_1h"] or 0,
            "x":              pricing.surcharge(r),
        })
    return out


def _session_to_dict(r, by_model=None, tz="local"):
    """Map a sessions-table row (joined with a ``tools`` GROUP_CONCAT column) to
    the JSON shape the dashboard and the /api/sessions endpoint both return.

    ``by_model`` is the {session_id: [...]} map from _session_model_breakdowns; the
    matching entry is attached so the client can price each model's tokens correctly.
    """
    try:
        t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0
    return {
        "session_id":      r["session_id"][:8],
        "full_session_id": r["session_id"],
        "project":         r["project_name"] or "unknown",
        "branch":          r["git_branch"] or "",
        "last":            (r["last_timestamp"] or "")[:16].replace("T", " "),
        "last_ts":         r["last_timestamp"] or "",  # raw ISO-UTC; client renders local
        # Local calendar day, not a UTC slice: the dashboard range-filters sessions
        # on this field against local bounds (app.js localISODate), the same bounds
        # the turn buckets above already use. A UTC slice here put a session and its
        # own turns on different sides of a range edge.
        "last_date":       _day_of(r["last_timestamp"], tz),
        "duration_min":    duration_min,
        "model":           r["model"] or "unknown",
        "topic":           r["topic"] or "",
        "tools":           [x for x in (r["tools"] or "").split(",") if x],
        "turns":           r["turn_count"] or 0,
        "input":           r["total_input_tokens"] or 0,
        "output":          r["total_output_tokens"] or 0,
        "cache_read":      r["total_cache_read"] or 0,
        "cache_creation":  r["total_cache_creation"] or 0,
        # Per-model token split so the client prices each model's tokens at its own
        # rate. A session that spawns a subagent on a different model (e.g. fable-5
        # main + opus Task) would otherwise be mispriced by applying the single
        # `model` label to the summed tokens. Empty when no breakdown was supplied.
        "by_model":        by_model.get(r["session_id"], []) if by_model else [],
    }


# Columns the sessions+tools query selects, shared by the full snapshot and the
# paginated endpoint so both feed _session_to_dict the same row shape.
_SESSION_SELECT = """
    SELECT
        s.session_id, s.project_name, s.first_timestamp, s.last_timestamp,
        s.total_input_tokens, s.total_output_tokens,
        s.total_cache_read, s.total_cache_creation, s.model, s.turn_count,
        s.git_branch, s.topic,
        COALESCE(t.tools, '') as tools
    FROM sessions s
    LEFT JOIN (
        SELECT
            session_id,
            GROUP_CONCAT(DISTINCT COALESCE(tool_name, '<none>')) as tools
        FROM turns
        GROUP BY session_id
    ) t ON t.session_id = s.session_id
"""

# Allowlisted sort keys for /api/sessions → ORDER BY fragment. Never interpolate
# raw user input into SQL; the query param only selects one of these fixed clauses.
_SESSION_SORTS = {
    "recent": "s.last_timestamp DESC",
    "oldest": "s.last_timestamp ASC",
    "tokens": "(s.total_input_tokens + s.total_output_tokens) DESC",
    "turns":  "s.turn_count DESC",
}


def _session_filters(project, model, start, end, tz="local"):
    """Build a parameterized WHERE clause for the sessions table from optional
    filters. Returns (clause_sql, params); every value is a bound ``?`` param."""
    clauses, params = [], []
    if project:
        clauses.append("s.project_name = ?"); params.append(project)
    if model:
        # Normalize like _session_to_dict / get_daily: NULL and '' both surface as
        # "unknown" in responses, so ?model=unknown must match those rows too.
        clauses.append("COALESCE(NULLIF(s.model, ''), 'unknown') = ?"); params.append(model)
    if start:
        clauses.append(_day_expr(tz, "s.last_timestamp") + " >= ?"); params.append(start)
    if end:
        clauses.append(_day_expr(tz, "s.last_timestamp") + " <= ?"); params.append(end)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_dashboard_data(db_path=None, tz="local"):
    # db_path defaults to the *current* module-level DB_PATH, looked up at call
    # time rather than baked into the signature at import time — a mutable
    # default here would ignore tests (and callers) that monkeypatch api.DB_PATH.
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = _connect(db_path)
    # Day buckets follow the display timezone the client asked for, so its range
    # bounds (app.js isoDate) and these aggregates share one calendar.
    day = _day_expr(tz)
    a_day = _day_expr(tz, "a.completed_at")

    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by local-day range) ──────
    # Timestamps are stored ISO-8601 UTC; date(timestamp, 'localtime') buckets by
    # the machine's local calendar day (DST-aware), matching the frontend's local
    # range bounds (app.js localISODate). Assumes the browser runs on the same
    # machine as the server — true for this localhost tool; a remote browser in a
    # different timezone would see an off-by-a-day ceiling, which is acceptable.
    # The substr fallback covers any malformed/unparseable timestamp.
    # Fast/US surcharge terms (x_*), priced client-side by pricing.js rowCost.
    turn_cols = _turn_cols(conn)
    X = pricing.surcharge_sql(turn_cols)
    daily_rows = conn.execute(f"""
        SELECT
            {day} as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(cache_creation_1h_tokens)  as cache_creation_1h,
            {X},
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "x":              pricing.surcharge(r),
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    # `day` is bucketed local (see daily_rows above) so it lines up with the range
    # filter; `hour` stays UTC on purpose — the client already TZ-shifts hours
    # (app.js), so local day + client-shifted UTC hour stay consistent together.
    hourly_rows = conn.execute(f"""
        SELECT
            {day} as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(NULLIF(model, ''), 'unknown')    as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── Tool usage per-day per-model (client filters by local-day range) ──────
    tool_rows = conn.execute(f"""
        SELECT
            {day} as day,
            COALESCE(model, 'unknown')      as model,
            COALESCE(tool_name, '<none>')   as tool,
            SUM(input_tokens)               as input,
            SUM(output_tokens)              as output,
            SUM(cache_read_tokens)          as cache_read,
            SUM(cache_creation_tokens)      as cache_creation,
            SUM(cache_creation_1h_tokens)       as cache_creation_1h,
            {X},
            COUNT(*)                        as turns
        FROM turns
        GROUP BY day, model, tool
        ORDER BY day, model, tool
    """).fetchall()

    tool_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "tool":           r["tool"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "x":              pricing.surcharge(r),
        "turns":          r["turns"] or 0,
    } for r in tool_rows]

    # ── Reasoning effort per-day per-model (client filters by local-day range) ─
    # Same shape as tool_by_model so the client prices it the same way (per
    # day+model row, then sums). NULL (migrated rows) and '' (pre-2.1.215 Claude
    # turns, and Codex sessions that never announced an effort) must collapse into
    # ONE 'unknown' bucket — hence the COALESCE/NULLIF on both sides.
    effort_rows = conn.execute(f"""
        SELECT
            {day} as day,
            COALESCE(NULLIF(model, ''), 'unknown')  as model,
            COALESCE(NULLIF(effort, ''), 'unknown') as effort,
            SUM(input_tokens)               as input,
            SUM(output_tokens)              as output,
            SUM(cache_read_tokens)          as cache_read,
            SUM(cache_creation_tokens)      as cache_creation,
            SUM(cache_creation_1h_tokens)       as cache_creation_1h,
            {X},
            COUNT(*)                        as turns
        FROM turns
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown'),
                 COALESCE(NULLIF(effort, ''), 'unknown')
        ORDER BY day, model, effort
    """).fetchall()

    effort_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "effort":         r["effort"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "x":              pricing.surcharge(r),
        "turns":          r["turns"] or 0,
    } for r in effort_rows]

    # ── MCP server per-day per-model (same shape as effort_by_model) ──────────
    # Only turns that consumed an MCP tool result; built-in tools have no server.
    mcp_rows = conn.execute(f"""
        SELECT
            {day} as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            mcp_server                      as mcp,
            SUM(input_tokens)               as input,
            SUM(output_tokens)              as output,
            SUM(cache_read_tokens)          as cache_read,
            SUM(cache_creation_tokens)      as cache_creation,
            SUM(cache_creation_1h_tokens)   as cache_creation_1h,
            {X},
            COUNT(*)                        as turns
        FROM turns
        WHERE mcp_server IS NOT NULL AND mcp_server != ''
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown'), mcp_server
    """).fetchall() if "mcp_server" in turn_cols else []

    mcp_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "mcp":            r["mcp"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "x":              pricing.surcharge(r),
        "turns":          r["turns"] or 0,
    } for r in mcp_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute(
        _SESSION_SELECT + " ORDER BY s.last_timestamp DESC"
    ).fetchall()
    by_model = _session_model_breakdowns(conn)
    sessions_all = [_session_to_dict(r, by_model, tz) for r in session_rows]

    # ── Subagent dispatches (Task/Agent tool) ─────────────────────────────────
    # The full ranked dispatch list, newest first — the client filters by the
    # active date range (on `day`, like the tool/daily rows) and derives both the
    # by-type chart and the top-dispatches table from it (same client-aggregation
    # pattern as tool_by_model). Sourced from the `agents` table, which only the
    # Claude source ever writes — so Codex rows (agent='codex') cannot leak in.
    # LEFT JOIN sessions for a friendly project label. The table is absent on DBs
    # created before this migration; degrade to an empty list rather than 500.
    subagent_dispatches = []
    try:
        dispatch_rows = conn.execute(f"""
            SELECT
                a.agent_id                       as agent_id,
                COALESCE(a.agent_type, 'unknown') as agent_type,
                a.dispatched_in_session          as session_id,
                COALESCE(s.project_name, 'unknown') as project,
                CASE WHEN a.completed_at IS NULL OR a.completed_at = '' THEN ''
                     ELSE {a_day} END as day,
                a.completed_at                   as completed_at,
                COALESCE(a.status, '')           as status,
                COALESCE(a.total_tokens, 0)      as total_tokens,
                COALESCE(a.total_duration_ms, 0) as total_duration_ms,
                COALESCE(a.tool_use_count, 0)    as tool_use_count
            FROM agents a
            LEFT JOIN sessions s ON s.session_id = a.dispatched_in_session
            ORDER BY a.total_tokens DESC, a.completed_at DESC
        """).fetchall()
        subagent_dispatches = [{
            "agent_id":          r["agent_id"],
            "agent_type":        r["agent_type"],
            "session_id":        r["session_id"] or "",
            "project":           r["project"],
            "day":               r["day"] or "",
            "completed_at":      r["completed_at"] or "",
            "total_tokens":      r["total_tokens"] or 0,
            "total_duration_ms": r["total_duration_ms"] or 0,
            "tool_use_count":    r["tool_use_count"] or 0,
        } for r in dispatch_rows]
    except sqlite3.OperationalError:
        pass  # pre-migration DB: no agents table yet

    conn.close()

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "tool_by_model":   tool_by_model,
        "effort_by_model": effort_by_model,
        "mcp_by_model":    mcp_by_model,
        "sessions_all":    sessions_all,
        "subagent_dispatches": subagent_dispatches,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_status(db_path=None):
    """First-ten-minutes state: roots found, scan progress, and an actionable
    empty-state hint (``empty`` is None once there is data)."""
    db_path = db_path or DB_PATH
    roots = config.discovered_roots(scanrun.projects_dir)
    sessions = 0
    if Path(db_path).exists():
        conn = _connect(db_path)
        try:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        except sqlite3.Error:
            pass  # DB not initialised yet
        finally:
            conn.close()
    claude = [r for r in roots if r["source"] == "claude" and r["exists"]]
    codex = [r for r in roots if r["source"] == "codex" and r["exists"]]
    empty = hint = None
    if not sessions:
        if not claude and not codex:
            empty, hint = "no_transcripts", (
                "No Claude Code or Codex transcripts found. Use Claude Code or Codex once, or point "
                "whytokens at them: --projects-dir PATH, WHYTOKENS_EXTRA_PROJECTS_DIRS=a;b, CODEX_HOME=PATH.")
        elif not claude:
            empty, hint = "codex_only", (
                "Only Codex sessions were looked for and none had usage yet. No Claude Code folder was "
                "found; if yours is elsewhere set WHYTOKENS_EXTRA_PROJECTS_DIRS.")
        else:
            empty, hint = "no_sessions", "Transcript folders exist but hold no usage yet."
    return {"roots": roots, "sessions": sessions, "empty": empty, "hint": hint,
            "scan": scanrun.status()}


def get_session_detail(session_id, db_path=None):
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}
    if not session_id:
        return {"error": "Missing session_id"}

    conn = _connect(db_path)

    session = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch
        FROM sessions
        WHERE session_id = ?
    """, (session_id,)).fetchone()

    if session is None:
        conn.close()
        return {"error": "Session not found"}

    turn_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(turns)").fetchall()
    }

    def turn_expr(col, default):
        return col if col in turn_cols else f"{default} as {col}"

    turns = conn.execute(f"""
        SELECT
            timestamp, COALESCE(model, 'unknown') as model,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens,
            {turn_expr('cache_creation_5m_tokens', '0')},
            {turn_expr('cache_creation_1h_tokens', '0')},
            {turn_expr('duration_ms', '0')},
            {turn_expr('stop_reason', "''")},
            {turn_expr('service_tier', "''")},
            {turn_expr('inference_geo', "''")},
            {turn_expr('is_sidechain', '0')},
            {turn_expr('is_compact_summary', '0')},
            {turn_expr('tool_calls', 'NULL')},
            {turn_expr('estimated', '0')},
            turns.agent_id as agent_id, agents.agent_type as agent_type,
            COALESCE(tool_name, '<none>') as tool_name,
            cwd,
            {pricing.surcharge_sql(turn_cols, agg=False)}
        FROM turns LEFT JOIN agents ON agents.agent_id = turns.agent_id
        WHERE session_id = ?
        ORDER BY timestamp, turns.id
    """, (session_id,)).fetchall()

    try:
        t1 = datetime.fromisoformat(session["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(session["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    # ponytail: gap_s is per actor (a subagent turn doesn't reset the main thread's idle time)
    last, traced = {}, []
    for r in turns:
        actor = (r["agent_type"] or r["agent_id"]) if r["agent_id"] else "main"
        gap = None
        try:
            t = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            key = r["agent_id"] or "main"
            if key in last:
                gap = round((t - last[key]).total_seconds(), 1)
            last[key] = t
        except (ValueError, AttributeError):
            pass
        traced.append((r, actor, gap))

    result = {
        "session": {
            "session_id":       session["session_id"],
            "display_id":       session["session_id"][:8],
            "project":          session["project_name"] or "unknown",
            "branch":           session["git_branch"] or "",
            "first":            (session["first_timestamp"] or "")[:16].replace("T", " "),
            "last":             (session["last_timestamp"] or "")[:16].replace("T", " "),
            "duration_min":     duration_min,
            "model":            session["model"] or "unknown",
            "turns":            session["turn_count"] or 0,
            "input":            session["total_input_tokens"] or 0,
            "output":           session["total_output_tokens"] or 0,
            "cache_read":       session["total_cache_read"] or 0,
            "cache_creation":   session["total_cache_creation"] or 0,
        },
        "turns": [{
            "timestamp":      (r["timestamp"] or "")[:16].replace("T", " "),
            "ts":             r["timestamp"] or "",
            "actor":          actor,
            "agent_id":       r["agent_id"] or "",
            "estimated":      bool(r["estimated"]),
            "context":        (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) + (r["cache_creation_tokens"] or 0),
            "gap_s":          gap,
            "model":          r["model"],
            "input":          r["input_tokens"] or 0,
            "output":         r["output_tokens"] or 0,
            "cache_read":     r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "cache_creation_5m": r["cache_creation_5m_tokens"] or 0,
            "cache_creation_1h": r["cache_creation_1h_tokens"] or 0,
            "duration_ms":    r["duration_ms"] or 0,
            "stop_reason":    r["stop_reason"] or "",
            "service_tier":   r["service_tier"] or "",
            "inference_geo":  r["inference_geo"] or "",
            "is_sidechain":   bool(r["is_sidechain"]),
            "is_compact_summary": bool(r["is_compact_summary"]),
            "tool":           r["tool_name"],
            "tool_calls":     json.loads(r["tool_calls"]) if r["tool_calls"] else [],
            "cwd":            r["cwd"] or "",
            "x":              pricing.surcharge(r),
        } for r, actor, gap in traced],
    }
    actors = {}
    for t in result["turns"]:
        a = actors.setdefault(t["actor"], {"actor": t["actor"], "turns": 0, "estimated_turns": 0,
                                           "peak_context": 0, "output": 0})
        a["turns"] += 1
        a["estimated_turns"] += t["estimated"]
        a["peak_context"] = max(a["peak_context"], t["context"])
        a["output"] += t["output"]
    result["actors"] = sorted(actors.values(), key=lambda a: -a["turns"])
    conn.close()
    return result


def get_waste(db_path=None):
    """Waste tab payload: findings, per-cause rollup, measurement status, one pricing basis."""
    import waste
    if db_path is None:
        db_path = DB_PATH
    if not Path(db_path).exists():
        return {"basis": waste.BASIS, "findings": [], "rollup": [], "total_usd": 0.0,
                "measurement": {"sources": [], "causes": {}}}
    # ponytail: whole-DB report per request; add a date filter / cache if a large corpus is slow
    findings = waste.report(db_path)
    rollup = {}
    for f in findings:
        r = rollup.setdefault(f["cause"], {"cause": f["cause"], "tier": f["tier"], "usd": 0.0, "sessions": 0})
        r["usd"] += f["usd"]
        r["sessions"] += 1
    return {"basis": waste.BASIS, "findings": findings, "total_usd": sum(f["usd"] for f in findings),
            "rollup": sorted(rollup.values(), key=lambda r: r["usd"], reverse=True),
            "measurement": waste.measurement(db_path)}


def get_session_breakdown(session_id, db_path=None):
    """Per-actor token split for one session: main thread vs each dispatched
    subagent type (and, where recorded, each skill).

    Buckets are disjoint and sum to the session total, resolved in priority order
    agent > skill > main: a turn produced inside a subagent belongs to that agent
    even when a skill was active in the parent, which is what makes the rows add up.

    Tokens only — cost is computed client-side so the dashboard's editable pricing
    table keeps working (see the module docstring). Rows are per (bucket, model)
    because a single agent type can span models and each turn must be priced with
    its own.
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}
    if not session_id:
        return {"error": "Missing session_id"}

    conn = _connect(db_path)
    turn_cols = {row["name"] for row in
                 conn.execute("PRAGMA table_info(turns)").fetchall()}
    # Older DBs predate these columns; degrade to "everything is main thread"
    # rather than failing the request.
    agent_join = ("LEFT JOIN agents a ON a.agent_id = t.agent_id"
                  if "agent_id" in turn_cols else "")
    agent_label = "a.agent_type" if "agent_id" in turn_cols else "NULL"
    skill_label = "t.skill" if "skill" in turn_cols else "NULL"

    rows = conn.execute(f"""
        SELECT
            CASE WHEN {agent_label} IS NOT NULL THEN 'agent'
                 WHEN {skill_label} IS NOT NULL AND {skill_label} != '' THEN 'skill'
                 ELSE 'main' END                                   AS kind,
            COALESCE({agent_label}, NULLIF({skill_label}, ''), 'main thread') AS label,
            COALESCE(t.model, 'unknown')                           AS model,
            COUNT(*)                                               AS turns,
            SUM(t.input_tokens)                                    AS input,
            SUM(t.output_tokens)                                   AS output,
            SUM(t.cache_read_tokens)                               AS cache_read,
            SUM(t.cache_creation_tokens)                           AS cache_creation,
            SUM({'t.cache_creation_5m_tokens' if 'cache_creation_5m_tokens' in turn_cols else '0'}) AS cache_creation_5m,
            SUM({'t.cache_creation_1h_tokens' if 'cache_creation_1h_tokens' in turn_cols else '0'}) AS cache_creation_1h,
            {pricing.surcharge_sql(turn_cols, 't.')}
        FROM turns t
        {agent_join}
        WHERE t.session_id = ?
        GROUP BY kind, label, model
        ORDER BY label, model
    """, (session_id,)).fetchall()
    conn.close()

    return {"session_id": session_id, "rows": [{
        "kind":              r["kind"],
        "label":             r["label"],
        "model":             r["model"],
        "turns":             r["turns"] or 0,
        "input":             r["input"] or 0,
        "output":            r["output"] or 0,
        "cache_read":        r["cache_read"] or 0,
        "cache_creation":    r["cache_creation"] or 0,
        "cache_creation_5m": r["cache_creation_5m"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "x":                 pricing.surcharge(r),
    } for r in rows]}


def _live_transcripts(session_id="", cwd=""):
    """Resolve a live session to its transcript files: the main ``<id>.jsonl``
    plus any ``<id>/subagents/*.jsonl`` (subagent records carry the *parent*
    sessionId, and the scanner rolls them into the same session row, so the live
    read must include them or it undercounts every agent-heavy session).

    Resolution mirrors the statusline's lib/ctx.mjs: by session id, else the
    newest transcript for a cwd. Returns (session_id, [paths]).
    """
    import glob
    # Through config.claude_projects_dirs, like every other default-path scan
    # site: with WHYTOKENS_EXTRA_PROJECTS_DIRS set, a session the scanner
    # ingests fine must not come back "not found" here.
    roots = [str(d) for d in config.claude_projects_dirs(scanner.DEFAULT_PROJECTS_DIRS)]
    main = None
    if session_id:
        # Guard the glob: the id is interpolated into a filesystem path.
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id):
            return "", []
        for root in roots:
            hits = glob.glob(os.path.join(root, "*", session_id + ".jsonl"))
            if hits:
                main = hits[0]
                break
    elif cwd:
        # Claude Code's project-dir mangling: EVERY non-alphanumeric character
        # becomes '-', not just the separators. Checked against 114 real project
        # dirs: a ':\/'-only rule matches 53 of them and misses
        # every path containing a '.' or '_' (C:\work\.tools\x -> C--work--tools-x).
        slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        hits = [p for root in roots
                for p in glob.glob(os.path.join(root, slug, "*.jsonl"))]
        if hits:
            main = max(hits, key=os.path.getmtime)
            session_id = os.path.basename(main)[:-len(".jsonl")]
    if not main or not os.path.exists(main):
        return session_id, []
    subs = sorted(glob.glob(os.path.join(main[:-len(".jsonl")], "subagents", "*.jsonl")))
    return session_id, [main] + subs


def get_live_session(session_id="", cwd=""):
    """Stats for a session read STRAIGHT FROM ITS TRANSCRIPT — no DB, no scan.

    The DB by definition lags the session you are sitting in, so hooks and the
    session-cost skill read the transcript directly. Keys mirror the ``session``
    object of /api/session (plus cost/context) so the two are comparable.
    """
    session_id, files = _live_transcripts(session_id, cwd)
    if not files:
        return {"error": "No transcript found for that session_id/cwd"}

    turns, metas = [], []
    for f in files:
        m, t, _ = scanner.parse_jsonl_file(f)
        turns.extend(t)
        metas.extend(m)
    if not turns:
        return {"error": "Transcript has no usage records yet"}

    cost = sum(pricing.calc_cost(
        t.get("model") or "unknown", t.get("input_tokens", 0), t.get("output_tokens", 0),
        t.get("cache_read_tokens", 0), t.get("cache_creation_tokens", 0),
        t.get("timestamp"), t.get("cache_creation_1h_tokens", 0))
        * pricing.price_mult(t.get("speed"), t.get("inference_geo"), t.get("model"),
                             t.get("input_tokens", 0) + t.get("cache_read_tokens", 0)
                             + t.get("cache_creation_tokens", 0)) for t in turns)

    stamps = sorted(t.get("timestamp") or "" for t in turns)
    try:
        t1 = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    # Context = the newest *main-chain* turn's input side; a subagent's context is
    # its own, and does not count against this session's replay cost.
    main = [t for t in turns if not t.get("is_sidechain")] or turns
    last = max(main, key=lambda t: t.get("timestamp") or "")
    context = (last.get("input_tokens", 0) + last.get("cache_read_tokens", 0)
               + last.get("cache_creation_tokens", 0))

    # Primary model by scanner.reconcile_session_models' rule, so the label matches
    # the dashboard row: main-chain, non-advisor turns; highest priority, then turn
    # count, then name.
    votes = collections.Counter(
        t["model"] for t in turns
        if t.get("model") and not t.get("is_sidechain")
        and not scanner.is_agent_summary_turn(t)
        and not (t.get("message_id") or "").startswith(scanner.ADVISOR_ID_PREFIX))
    model = max(votes, key=lambda m: (scanner._model_priority(m), votes[m], m))         if votes else "unknown"

    def tot(k):
        return sum(t.get(k, 0) or 0 for t in turns)

    meta = metas[0] if metas else {}

    return {
        "session_id":     session_id,
        "display_id":     session_id[:8],
        "project":        meta.get("project_name") or scanner.project_name_from_cwd(last.get("cwd") or ""),
        "branch":         meta.get("git_branch") or "",
        "first":          stamps[0][:19].replace("T", " "),
        "last":           stamps[-1][:19].replace("T", " "),
        "duration_min":   duration_min,
        "model":          model,
        "turns":          len(turns),
        "input":          tot("input_tokens"),
        "output":         tot("output_tokens"),
        "cache_read":     tot("cache_read_tokens"),
        "cache_creation": tot("cache_creation_tokens"),
        "cost":           round(cost, 4),
        "cost_per_turn":  round(cost / len(turns), 4),
        "context_tokens": context,
    }


def get_sessions_page(project="", model="", start="", end="", limit=50,
                      offset=0, sort="recent", tz="local", db_path=None):
    """Paginated, SQL-side-filtered session list (the "hub" read endpoint).

    Filters (all optional): project name, primary model, and a [start, end]
    date window (inclusive, YYYY-MM-DD, matched against last_timestamp). The page
    is sorted by an allowlisted key and sliced with LIMIT/OFFSET in SQL — the
    server never ships the whole table for an external caller to page through.
    Returns {sessions, total, limit, offset, sort, filters}. ``total`` is the
    full filtered count (pre-pagination) so callers can build pagers.
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    # Clamp pagination to sane bounds; coerce junk to the defaults.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    order_by = _SESSION_SORTS.get(sort, _SESSION_SORTS["recent"])
    if sort not in _SESSION_SORTS:
        sort = "recent"

    where, params = _session_filters(project, model, start, end, tz)

    conn = _connect(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM sessions s {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"{_SESSION_SELECT} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        by_model = _session_model_breakdowns(
            conn, [r["session_id"] for r in rows]
        )
    finally:
        conn.close()

    return {
        "sessions": [_session_to_dict(r, by_model, tz) for r in rows],
        "total":    total,
        "limit":    limit,
        "offset":   offset,
        "sort":     sort,
        "filters":  {"project": project or "", "model": model or "",
                     "start": start or "", "end": end or ""},
    }


def get_daily(start="", end="", model="", project="", tz="", db_path=None):
    """SQL-side daily token aggregation with server-side cost.

    Groups turns by (day, model) in SQL — the aggregation happens in SQLite, not
    in the client — applying optional model/project/date filters. Cost is added
    per (day, model) bucket using pricing.calc_cost so the single-source price
    table is honored server-side. Returns {daily, totals, filters}.

    ``day`` is a documented external contract (docs/SCHEMA.md) and defaults to
    UTC bucketing (substr(timestamp, 1, 10)) to keep that contract stable; pass
    ``tz=local`` to bucket by the server machine's local calendar day instead
    (date(timestamp, 'localtime') — DST-aware, matches the dashboard's own
    range bounds). start/end filters stay UTC-compared either way.
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    day_expr = ("COALESCE(date(t.timestamp, 'localtime'), substr(t.timestamp, 1, 10))"
                if tz == "local" else "substr(t.timestamp, 1, 10)")

    clauses, params = [], []
    if start:
        clauses.append("substr(t.timestamp, 1, 10) >= ?"); params.append(start)
    if end:
        clauses.append("substr(t.timestamp, 1, 10) <= ?"); params.append(end)
    if model:
        clauses.append("COALESCE(NULLIF(t.model, ''), 'unknown') = ?"); params.append(model)
    # A project filter needs the session's project_name; only join when asked.
    join = ""
    if project:
        join = "JOIN sessions s ON s.session_id = t.session_id"
        clauses.append("s.project_name = ?"); params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = _connect(db_path)
    try:
        # GROUP/ORDER by position (1=day, 2=model): a project filter joins the
        # sessions table, which also has a `model` column, so the bare name is
        # ambiguous — the positional refs bind to the SELECT's day/model exprs.
        rows = conn.execute(f"""
            SELECT
                {day_expr}                               as day,
                COALESCE(NULLIF(t.model, ''), 'unknown') as model,
                SUM(t.input_tokens)                      as input,
                SUM(t.output_tokens)                     as output,
                SUM(t.cache_read_tokens)                 as cache_read,
                SUM(t.cache_creation_tokens)             as cache_creation,
                SUM(t.cache_creation_1h_tokens)              as cache_creation_1h,
                {pricing.surcharge_sql(_turn_cols(conn), 't.')},
                COUNT(*)                                 as turns
            FROM turns t
            {join}
            {where}
            GROUP BY 1, 2
            ORDER BY 1, 2
        """, params).fetchall()
    finally:
        conn.close()

    daily, tot_in, tot_out, tot_cr, tot_cc, tot_turns, tot_cost = [], 0, 0, 0, 0, 0, 0.0
    for r in rows:
        inp = r["input"] or 0
        out = r["output"] or 0
        cr = r["cache_read"] or 0
        cc = r["cache_creation"] or 0
        cc1h = r["cache_creation_1h"] or 0
        cost = (pricing.calc_cost(r["model"], inp, out, cr, cc, r["day"],
                                  cache_creation_1h=cc1h) +
                pricing.surcharge_cost(r["model"], r, r["day"]))
        tot_in += inp; tot_out += out; tot_cr += cr; tot_cc += cc
        tot_turns += r["turns"] or 0; tot_cost += cost
        daily.append({
            "day":            r["day"],
            "model":          r["model"],
            "input":          inp,
            "output":         out,
            "cache_read":     cr,
            "cache_creation": cc,
            "cache_creation_1h": cc1h,
            "turns":          r["turns"] or 0,
            "cost":           round(cost, 6),
        })

    return {
        "daily":   daily,
        "totals":  {"input": tot_in, "output": tot_out, "cache_read": tot_cr,
                    "cache_creation": tot_cc, "turns": tot_turns,
                    "cost": round(tot_cost, 6)},
        "filters": {"start": start or "", "end": end or "",
                    "model": model or "", "project": project or "",
                    "tz": "local" if tz == "local" else "utc"},
    }


def find_icon_file():
    """Locate the header icon.svg (repo-root ``resources/icon.svg``).

    Returns the path, or ``None`` so the /icon.svg route can 404 gracefully
    (the header ``<img>`` then just renders empty alt text).
    """
    candidate = Path(__file__).resolve().parent / "resources" / "icon.svg"
    return candidate if candidate.is_file() else None


def resolve_static(rel_path):
    """Map a "/static/<rel_path>" request to a file inside STATIC_ROOT.

    Returns the resolved Path if it stays inside STATIC_ROOT and is a real file,
    else None. Guards against path traversal (``../``) and absolute/symlinked
    escapes by resolving the candidate and confirming STATIC_ROOT is a parent.
    """
    root = STATIC_ROOT.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
TOKEN_HEADER = "X-Whytokens-Token"


def _host_name(netloc):
    """Hostname of a Host header / bind address: port and IPv6 brackets stripped."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[1:].split("]", 1)[0]
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


class DashboardHandler(BaseHTTPRequestHandler):
    # Set by serve(). None fails closed: every POST is refused until a token exists.
    token = None
    allow_any_host = False   # serve(unsafe_host=True) only
    extensions = []          # set by serve(); loaded descriptors (extensions.py)

    def _refuse(self, why):
        self._send_bytes(why.encode(), "text/plain", status=403)
        return False

    def _guard(self, post=False):
        """Host must be loopback (DNS rebinding), Origin same-origin, POST needs the token."""
        host = self.headers.get("Host")
        if not self.allow_any_host and _host_name(host) not in LOOPBACK_HOSTS:
            return self._refuse("bad host")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            return self._refuse("bad origin")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._refuse("cross-site")
        if post:
            sent = self.headers.get(TOKEN_HEADER) or ""
            if not self.token or not secrets.compare_digest(sent, self.token):
                return self._refuse("bad token")
        return True

    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    # ── small response helpers ───────────────────────────────────────────────
    def _send_bytes(self, body, content_type, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self._send_bytes(body, "application/json", status)

    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def _serve_static_file(self, target, cache=False):
        ext = target.suffix.lower()
        content_type = STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
        body = target.read_bytes()
        # Long-cache only immutable assets (vendored libs, icon); serve our own
        # source (HTML/CSS/JS, which changes between edits) with no-cache so a
        # plain reload always reflects changes — no hard-refresh needed.
        extra = {"Cache-Control": "max-age=86400" if cache else "no-cache"}
        self._send_bytes(body, content_type, extra_headers=extra)

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            index = resolve_static("index.html")
            if index is None:
                self._send_404()
                return
            # the page carries the startup token that authorizes POSTs
            meta = f'<meta name="whytokens-token" content="{self.token or ""}">'.encode()
            body = index.read_bytes().replace(b"</head>", meta + b"</head>", 1)
            self._send_bytes(body, "text/html; charset=utf-8", extra_headers={"Cache-Control": "no-cache"})

        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = resolve_static(rel)
            if target is None:
                self._send_404()
                return
            # Only vendored assets (e.g. Chart.js) are immutable enough to long-cache.
            self._serve_static_file(target, cache=rel.startswith("vendor/"))

        elif path == "/api/data":
            self._send_json(get_dashboard_data(tz=_qs1(parse_qs(parsed.query), "tz")))

        elif path == "/api/session":
            params = parse_qs(parsed.query)
            session_id = params.get("session_id", [""])[0]
            self._send_json(get_session_detail(session_id))

        elif path == "/api/session-breakdown":
            params = parse_qs(parsed.query)
            self._send_json(get_session_breakdown(
                params.get("session_id", [""])[0]))

        elif path == "/api/sessions":
            q = parse_qs(parsed.query)
            self._send_json(get_sessions_page(
                project=_qs1(q, "project"), model=_qs1(q, "model"),
                start=_qs1(q, "start"), end=_qs1(q, "end"),
                limit=_qs1(q, "limit", "50"), offset=_qs1(q, "offset", "0"),
                sort=_qs1(q, "sort", "recent"), tz=_qs1(q, "tz"),
            ))

        elif path == "/api/daily":
            q = parse_qs(parsed.query)
            self._send_json(get_daily(
                start=_qs1(q, "start"), end=_qs1(q, "end"),
                model=_qs1(q, "model"), project=_qs1(q, "project"),
                tz=_qs1(q, "tz"),
            ))

        elif path == "/api/live":
            # Current-session stats read from the transcript, bypassing the DB
            # (which cannot know about the session you are in). Feeds the
            # session-cost skill and the cost band in the UserPromptSubmit hook.
            q = parse_qs(parsed.query)
            self._send_json(get_live_session(
                session_id=_qs1(q, "session_id"), cwd=_qs1(q, "cwd"),
            ))

        elif path == "/api/status":
            self._send_json(get_status())

        elif path == "/api/waste":
            self._send_json(get_waste())

        elif path == "/api/pricing":
            # The single source of truth for the price table, served so the
            # frontend never hardcodes the numbers (pricing.py owns them).
            self._send_json(pricing.PRICING)

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self._send_404()
                return
            self._serve_static_file(icon, cache=True)

        elif path == "/api/extensions":
            self._send_json({"tabs": [
                {"name": t["name"], "label": t["label"],
                 "module_url": f"/ext/{e['name']}/{t['module']}"}
                for e in self.extensions for t in e["tabs"]]})

        elif path.startswith("/ext/"):
            name, _, rel = path[len("/ext/"):].partition("/")
            ext = next((e for e in self.extensions if e["name"] == name), None)
            target = extensions.static_file(ext, rel) if ext and rel else None
            if target is None:
                self._send_404()
                return
            self._serve_static_file(target)

        else:
            for e in self.extensions:
                for route, fn in e["routes"]:
                    if path == route:
                        self._send_json(fn(parse_qs(parsed.query), DB_PATH))
                        return
            self._send_404()

    def do_POST(self):
        if not self._guard(post=True):
            return
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Incremental rescan: same engine as `python cli.py scan` (Claude +
            # Codex via sources.scan_all), just triggered from the dashboard. No
            # DB unlink — usage.db is the only durable record of usage once Claude
            # Code prunes old transcripts, so a wipe-and-rebuild could permanently
            # lose history no longer on disk. Non-destructive also means satellite
            # tables (e.g. an extension's) are untouched by
            # construction; no snapshot/restore needed. DB_PATH is read from the
            # module global so tests that patch it are honored; the sources resolve
            # their scan roots (scanner.DEFAULT_PROJECTS_DIRS /
            # sources.codex.CODEX_PATH) lazily at discover time for the same reason.
            # Serialized behind scanrun's lock: a click during the startup scan
            # reports running instead of racing it. Flat keys stay for the frontend.
            st = scanrun.start(DB_PATH, wait=True)
            running = st is None
            if running:
                st = scanrun.status()
            result = dict(st["result"] or {})
            for k in ("new", "updated", "skipped", "turns", "sessions"):
                result.setdefault(k, 0)
            result.update(running=running, error=st["error"], file_errors=st["file_errors"])
            self._send_json(result)
        else:
            self._send_404()


def serve(host=None, port=None, unsafe_host=False, scan=False, projects_dir=None):
    """Serve the dashboard. ``scan=True`` streams a scan in the background after the
    port is bound, so the page is up (and shows progress) before the first scan ends."""
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", str(DEFAULT_PORT)))
    if _host_name(host) not in LOOPBACK_HOSTS and not unsafe_host:
        raise SystemExit(f"Refusing to listen on {host!r}: the dashboard serves your usage "
                         "data and can trigger rescans. Pass --unsafe-host to override.")
    config.ensure_db(DB_PATH)
    DashboardHandler.extensions = extensions.load()
    DashboardHandler.token = secrets.token_urlsafe(32)
    DashboardHandler.allow_any_host = unsafe_host
    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as e:
        raise SystemExit(f"Cannot listen on {host}:{port} ({e.strerror or e}). Is whytokens already "
                         f"running? Pick another port with --port.")
    if scan:
        scanner.init_db(scanner.get_db(DB_PATH))  # empty DB, so the page loads before the scan lands
        scanrun.start(DB_PATH, projects_dir)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
