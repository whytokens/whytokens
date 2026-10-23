"""
cli.py - Command-line interface for whytokens.

Commands:
  scan      - Scan JSONL files and update the database (`--doctor` reports drift)
  doctor    - Report schema drift (unknown keys / unseen versions); writes nothing
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta

# Pricing lives in one place only (pricing.py). Re-exported here so callers
# can `from cli import PRICING, get_pricing, calc_cost`.
from pricing import PRICING, get_pricing, calc_cost, surcharge_cost, surcharge_sql


def row_cost(r, timestamp=None):
    """Cost of one aggregated query row. Every cli query selects the same
    column aliases (inp/out/cr/cc/cc1h), so the 1h cache-write split is applied
    in exactly one place instead of at each call site. Each query also selects
    the fast/US surcharge terms (_surcharge), priced here at the row's rates."""
    return (calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0,
                      r["cc"] or 0, timestamp, cache_creation_1h=r["cc1h"] or 0) +
            surcharge_cost(r["model"], r, timestamp))


def dated_costs(conn, where="1=1", params=()):
    """{model: cost} for the turns matching ``where``, priced per UTC day so
    effective-dated tiers resolve on each turn's own date (not the bucket's,
    and not the display timezone's)."""
    costs = {}
    for r in conn.execute(f"""
            SELECT COALESCE(model, 'unknown') as model, substr(timestamp, 1, 10) as day,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   SUM(cache_creation_1h_tokens) as cc1h, {_surcharge(conn)}
            FROM turns WHERE {where} GROUP BY model, day""", params):
        costs[r["model"]] = costs.get(r["model"], 0.0) + row_cost(r, r["day"])
    return costs


def _surcharge(conn):
    return surcharge_sql({r[1] for r in conn.execute("PRAGMA table_info(turns)")})

# DB location + dashboard port live in one place (config.py) so this profiler
# can run alongside the original claude-usage tool without clashing.
import config
from config import DB_PATH, DEFAULT_PORT

# Timestamps are stored ISO-8601 UTC; these commands compare day buckets against
# Python's local `date.today()`, so bucketing must use SQLite's DST-aware
# 'localtime' modifier to match (a bare `substr` would be UTC and off by up to a
# day near midnight). Falls back to the UTC substr for any malformed timestamp.
LOCAL_DAY = "COALESCE(date(timestamp, 'localtime'), substr(timestamp, 1, 10))"

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    config.ensure_db(DB_PATH)
    if not DB_PATH.exists():
        print("Database not found. Run: whytokens scan")
        sys.exit(1)
    # get_db centralizes row_factory + WAL (so callers don't each set them);
    # init_db brings an older DB up to the current schema before we query it.
    # A read command that uses a column added by an additive migration
    # (`agent`, `reasoning_tokens`, ...) would raise "no such column" on a DB
    # last written before it. init_db is idempotent (CREATE ... IF NOT EXISTS +
    # additive ALTERs), so it is a cheap no-op once migrated.
    from scanner import get_db, init_db
    conn = get_db(DB_PATH)
    init_db(conn)
    return conn


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    import sources
    config.ensure_db(DB_PATH)
    # Scan every registered source (Claude Code + Codex) into config.DB_PATH.
    # `--projects-dir` overrides only the Claude location; Codex uses CODEX_PATH.
    sources.scan_all(db_path=DB_PATH, projects_dir=projects_dir)


def cmd_doctor(projects_dir=None):
    """Schema-drift report (read-only): scan the real transcript logs for both
    sources (Claude Code + Codex) and flag any unknown keys / record types (and, for
    Claude, versions not yet vetted), so silent envelope drift becomes visible.
    Writes nothing — see schema.py."""
    import schema
    import config
    from scanner import DEFAULT_PROJECTS_DIRS
    from config import CODEX_PATH

    dirs = ([Path(projects_dir)] if projects_dir
            else config.claude_projects_dirs(DEFAULT_PROJECTS_DIRS))
    claude_report = schema.doctor_scan(dirs)
    print(schema.format_doctor_report(claude_report))

    codex_report = schema.codex_doctor_scan([CODEX_PATH])
    print()
    print(schema.format_codex_doctor_report(codex_report))

    # Non-zero exit if EITHER source drifted, so CI / scripts can gate on it.
    if not (schema.report_is_clean(claude_report)
            and schema.codex_report_is_clean(codex_report)):
        sys.exit(2)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()

    rows = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {_surcharge(conn)},
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE {LOCAL_DAY} = ?
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    costs = dated_costs(conn, f"{LOCAL_DAY} = ?", (today,))

    for r in rows:
        cost = costs[r["model"]]
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute(f"""
        SELECT
            {LOCAL_DAY}                as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {_surcharge(conn)},
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()

    by_model = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {_surcharge(conn)},
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute(f"""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += row_cost(r, d)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    costs = dated_costs(conn, f"{LOCAL_DAY} BETWEEN ? AND ?", (start, end))
    for r in by_model:
        cost = costs[r["model"]]
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cache read:          {fmt(total_cr)}")
    print(f"  Cache creation:      {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute(f"""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {_surcharge(conn)},
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            SUM(cache_creation_1h_tokens) as cc1h,
            {_surcharge(conn)},
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Daily average (last 30 days)
    daily_avg = conn.execute(f"""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                {LOCAL_DAY} as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    costs = dated_costs(conn)
    total_cost = sum(costs.values())

    print()
    hr("=")
    print("  whytokens - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = costs[r["model"]]
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_waste(session_id=None):
    import waste
    conn = require_db()
    if session_id and not conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
        conn.close()
        print(f"unknown session {session_id}")
        return
    newest = conn.execute("SELECT MAX(timestamp) FROM turns").fetchone()[0]
    conn.close()
    findings = waste.report(DB_PATH, session_id)
    if not findings:
        print("No material waste found.")
        return
    print(f"amounts are {waste.BASIS['label']}")
    print(f"data through {newest}; run `whytokens scan` to refresh")
    print(f"{'session':<36} {'cause':<20} {'usd':>9}  detail  advice")
    for finding in findings:
        # full id: an 8-char prefix collided 93 times on a real DB and cannot round-trip into --session
        print(f"{finding['session_id']:<36} {finding['cause']:<20} ${finding['usd']:>8.4f}  {finding['detail']}  {finding['advice']}")


def cmd_live(session_id=None, cwd=None, as_json=False):
    """Stats for a session read straight from its transcript — no DB, no scan.

    Same numbers the dashboard row shows, but current: the DB cannot know about
    the session you are sitting in until the next scan. Defaults to the newest
    transcript for the current directory. `--json` is the machine-readable form
    the /profile skill and the cost hook consume."""
    import api
    data = api.get_live_session(session_id=session_id or "",
                                cwd=cwd or os.getcwd())
    if as_json:
        print(json.dumps(data, indent=2))
        return
    if "error" in data:
        print(data["error"])
        return
    print(f"{data['display_id']}  {data['project']}"
          f"{('  [' + data['branch'] + ']') if data['branch'] else ''}")
    print(f"  model    {data['model']}   turns {data['turns']}   "
          f"{data['duration_min']}m   last {data['last']}")
    print(f"  tokens   in {data['input']:,}  out {data['output']:,}  "
          f"cache-read {data['cache_read']:,}  cache-write {data['cache_creation']:,}")
    print(f"  context  {data['context_tokens']:,}")
    print(f"  cost     ${data['cost']:.4f}   (${data['cost_per_turn']:.4f}/turn)")


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False, unsafe_host=False):
    # The scan streams in the background once the port is bound (api.serve).
    from api import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", str(DEFAULT_PORT)))

    # Open a browser for users running this as a script (see README). The VS Code
    # extension passes --no-browser since it embeds the dashboard in a webview.
    if not no_browser:
        import webbrowser
        import threading
        import time

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, unsafe_host=unsafe_host, scan=True, projects_dir=projects_dir)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
whytokens: where your Claude Code and Codex tokens went, from local logs.

Usage:
  whytokens scan [--projects-dir PATH]   Scan JSONL files and update database
  whytokens scan --doctor [--projects-dir PATH]
                                         Report schema drift (unknown keys /
                                             unseen Claude Code versions); writes nothing
  whytokens doctor [--projects-dir PATH] Alias for `scan --doctor`
  whytokens today                        Show today's usage summary
  whytokens week                         Show last 7 days (per-day + by-model)
  whytokens stats                        Show all-time statistics
  whytokens live [--session ID] [--cwd PATH] [--json]
                                         Current session's stats straight from its
                                             transcript (no DB, no scan) - cost, context, tokens
  whytokens waste [--session ID]         Attribute material session spend to avoidable causes
  whytokens dashboard [--projects-dir PATH] [--host HOST] [--unsafe-host] [--port PORT] [--no-browser]
                                             Scan + start dashboard (opens a browser unless --no-browser)
"""

COMMANDS = {
    "scan": cmd_scan,
    "doctor": cmd_doctor,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "live": cmd_live,
    "waste": cmd_waste,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
        if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
            print(USAGE)
            sys.exit(0)

        command = sys.argv[1]
        rest = sys.argv[2:]
        projects_dir = parse_named_arg(rest, "--projects-dir")

        if command == "dashboard":
            cmd_dashboard(
                projects_dir=projects_dir,
                host=parse_named_arg(rest, "--host"),
                port=parse_named_arg(rest, "--port"),
                no_browser="--no-browser" in rest,
                unsafe_host="--unsafe-host" in rest,
            )
        elif command == "scan":
            # `scan --doctor` reports drift instead of ingesting.
            if "--doctor" in rest:
                cmd_doctor(projects_dir=projects_dir)
            else:
                cmd_scan(projects_dir=projects_dir)
        elif command == "doctor":
            cmd_doctor(projects_dir=projects_dir)
        elif command == "live":
            cmd_live(session_id=parse_named_arg(rest, "--session"),
                     cwd=parse_named_arg(rest, "--cwd"),
                     as_json="--json" in rest)
        elif command == "waste":
            cmd_waste(parse_named_arg(rest, "--session"))
        else:
            COMMANDS[command]()

if __name__ == "__main__":
    main()
