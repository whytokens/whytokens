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
import re
import sys
from pathlib import Path
from datetime import datetime, date, timedelta

# Pricing lives in one place only (pricing.py). Re-exported here so callers
# can `from cli import PRICING, get_pricing, calc_cost`.
import pricing
from pricing import PRICING, get_pricing, calc_cost, surcharge_cost, surcharge_sql, tier_day_sql


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


def cost_layers(conn, where="1=1", params=()):
    """{recorded, advisor, estimated, total: $, web_searches: n} for the turns matching ``where``,
    priced like dated_costs. The web-search fee joins estimated, dated by its parent turn."""
    out = dict.fromkeys(pricing.LAYERS, 0.0)
    for r in conn.execute(f"""
            SELECT COALESCE(model, 'unknown') as model, substr(timestamp, 1, 10) as day,
                   {pricing.layer_sql()} as layer,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   SUM(cache_creation_1h_tokens) as cc1h, {_surcharge(conn)}
            FROM turns WHERE {where} GROUP BY model, day, layer""", params):
        out[r["layer"]] += row_cost(r, r["day"])
    n = conn.execute(f"SELECT COUNT(*) FROM tool_results WHERE {pricing.WEB_SEARCH_OK} AND message_id IN"
                     f" (SELECT message_id FROM turns WHERE {where})", params).fetchone()[0]
    out["estimated"] += n * pricing.WEB_SEARCH_FEE_USD
    out["web_searches"] = n
    out["total"] = sum(out[k] for k in pricing.LAYERS)
    return out


def layer_line(L):
    """The one-liner every cost total carries (fixed layer names: docs/SCHEMA.md)."""
    return " · ".join(f"{k} {fmt_cost(L[k])}" for k in pricing.LAYERS)


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
    # Scan every registered source (Claude Code + Codex) into config.DB_PATH.
    # `--projects-dir` overrides only the Claude location; Codex uses CODEX_PATH.
    try:
        sources.scan_all(db_path=DB_PATH, projects_dir=projects_dir)
    except sources.ScanBusy as e:
        raise SystemExit(f"whytokens: {e}")


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

    import mcp_config  # configured MCP servers + duplicate installs (static, nothing launched)
    print()
    print(mcp_config.format_report(mcp_config.configured_servers(Path.home(), Path.cwd())))

    import pricing  # models billed at another row's rates: informational, never an exit code
    print()
    print(pricing.format_fallbacks(pricing.fallbacks_in_db(DB_PATH)))

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
    costs = dated_costs(conn, f"{LOCAL_DAY} = ?", (today,))
    layers = cost_layers(conn, f"{LOCAL_DAY} = ?", (today,))

    for r in rows:
        cost = costs[r["model"]]
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(layers['total'])}")
    print(f"  {'':<30}  {layer_line(layers)}")
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
            {tier_day_sql()}           as rate_day,
            COUNT(*)                   as turns
        FROM turns
        WHERE {LOCAL_DAY} BETWEEN ? AND ?
        GROUP BY day, model, rate_day
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
        bucket["cost"]  += row_cost(r, r["rate_day"])

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    costs = dated_costs(conn, f"{LOCAL_DAY} BETWEEN ? AND ?", (start, end))
    layers = cost_layers(conn, f"{LOCAL_DAY} BETWEEN ? AND ?", (start, end))
    for r in by_model:
        cost = costs[r["model"]]
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(layers['total'])}")
    print(f"    {'':<30}  {layer_line(layers)}")
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
    layers = cost_layers(conn)
    total_cost = layers["total"]

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
    print(f"                    {layer_line(layers)}")
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
    for cause, cells in waste.measurement(DB_PATH)["causes"].items():
        if cells.get("claude", {}).get("status") == "partial":  # e.g. retry_loop sessions with no transcript left
            print(f"{cause}: {cells['claude']['reason']}")
    print(f"{'session':<36} {'cause':<20} {'usd':>9}  detail  advice")
    for finding in findings:
        # full id: an 8-char prefix collided 93 times on a real DB and cannot round-trip into --session
        obs = "" if finding["in_total"] else "  (observation, not in any total)"
        print(f"{finding['session_id']:<36} {finding['cause']:<20} ${finding['usd']:>8.4f}{obs}  {finding['detail']}  {finding['advice']}")


def cmd_export(as_json=False):
    """Versioned JSON of sessions, waste and totals (docs/SCHEMA.md "Export document")."""
    if not as_json:
        print("usage: whytokens export --json")
        sys.exit(2)
    import api
    print(json.dumps(api.get_export(DB_PATH), indent=2))


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

def cmd_reread(rest):
    """Carried re-read cost by source, across sessions (an observation: never in any total)."""
    import waste
    from datetime import timezone  # turns.timestamp is UTC ISO
    days = float(parse_named_arg(rest, "--days") or 30)
    classes = []
    for i, arg in enumerate(rest):
        if arg == "--path-class":
            name, sep, rx = (rest[i + 1] if i + 1 < len(rest) else "").partition("=")
            if not (sep and name and rx):
                print("error: --path-class needs NAME=REGEX")
                sys.exit(2)
            try:
                classes.append((name, re.compile(rx, re.I)))
            except re.error as e:
                print(f"error: --path-class {name}: {e}")
                sys.exit(2)
    require_db().close()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    cwd = parse_named_arg(rest, "--cwd")
    r = waste.carried_rereads(DB_PATH, since=since, cwd=cwd, classes=classes)
    spend = r["spend_usd"] or 1.0
    print(f"carried re-reads, last {days:g} days{', cwd~' + cwd if cwd else ''}: {r['sessions']} sessions, "
          f"{r['lanes']} lanes; observation, overlaps resident_output/repeated_reads, not in any total")
    print(f"cache reads are ${r['cache_read_usd']:.2f} of ${r['spend_usd']:.2f} spend ({r['cache_read_usd'] / spend:.1%}), "
          f"{waste.BASIS['pricing']}; tool results are byte estimates")
    rows = list(r["by_source"].items())
    if len(rows) > 15:  # the long tail of rarely-used tools folds into one row
        rows = rows[:14] + [(f"(other {len(rows) - 14} tools)", sum(usd for _, usd in rows[14:]))]
    rows.append(("(rest: fixed prefix, user text, capped excess)", r["rest_usd"]))
    for label, usd in rows:
        print(f"  {label[:48]:<48} ${usd:>10.2f} {usd / spend:>7.1%}")
    if r["excluded_sessions"]:
        print(f"not measured: {r['excluded_sessions']} sessions with incomplete result sizes (stored before per-call "
              f"ingestion, transcripts gone), ${r['excluded_cache_read_usd']:.2f} of cache reads")


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
  whytokens export --json                Versioned JSON: sessions, waste, totals
  whytokens live [--session ID] [--cwd PATH] [--json]
                                         Current session's stats straight from its
                                             transcript (no DB, no scan) - cost, context, tokens
  whytokens card --out FILE.html|.svg [--with-projects]
                                         One aggregate-only file to forward: period, total, top 3 causes,
                                             one verified change. No names/paths/commands; --with-projects tags the change's project
  whytokens waste [--session ID]         Attribute material session spend to avoidable causes
  whytokens reread [--days N] [--cwd TEXT] [--path-class NAME=REGEX ...]
                                         Carried re-read cost by source (own output, tool results,
                                             Read by path class); an observation, never in a total
  whytokens fix record --project P --cause C --note TEXT [--date YYYY-MM-DD] [--file PATH]
                                         Record a waste fix (--file stores a hash of the CLAUDE.md/config changed)
  whytokens fix list                     List recorded fixes
  whytokens fix edit ID [--date D] [--note TEXT]   Correct a fix's date or note
  whytokens fix delete ID                Remove a recorded fix
  whytokens budget [AMOUNT|--clear]      Show this month's spend vs the monthly budget (USD, API list
                                             price); AMOUNT sets it, --clear removes it
  whytokens reconcile [--period YYYY-MM] [--billed AMOUNT [--as-of ISO]|--clear] [--json]
                                         Split the gap to your bill (UTC month) into named causes;
                                             --billed stores the billed figure (as of now, mid-month)
  whytokens reconcile --session ID [--usage AMOUNT]   One session vs its /usage cost
  whytokens shapes                       Cost by work shape (edit-heavy, shell-heavy, ...) and how many
                                             sessions cost 2x or more their peers
  whytokens project tag PROJECT [TAG|--clear]   Show, set or clear a project's display tag
  whytokens verify [FIX_ID]              Attributed share of spend for the fix's (project, cause),
                                             after vs before; needs 10 sessions each side
  whytokens llm [label|judge SESSION | draft SESSION CAUSE]
                                         Opt-in model help (claude -p): shows exactly what would be
                                             sent and its estimated cost, sends only on y; no args: spend
  whytokens pricing check [--offline]    Review diff of the shipped price table vs LiteLLM (OpenRouter
                                             cross-check). Network call only here; --offline uses the cache
  whytokens dashboard [--projects-dir PATH] [--host HOST] [--unsafe-host] [--port PORT] [--no-browser]
                                             Scan + start dashboard (opens a browser unless --no-browser)
"""


def usage(has_llm=None):
    """USAGE, without the `llm` command in a build that leaves model help out (public builds)."""
    if has_llm is None:
        has_llm = config.HAS_LLM
    return USAGE if has_llm else re.sub(r"  whytokens llm .*?\n(?=  whytokens )", "", USAGE, flags=re.S)

def cmd_card(rest):
    import sqlite3
    import card
    out = parse_named_arg(rest, "--out")
    if not out:
        print("usage: whytokens card --out FILE.html|.svg [--with-projects] [--utc]")
        sys.exit(2)
    try:
        require_db().close()
        card.write(DB_PATH, out, include_projects="--with-projects" in rest, tz="utc" if "--utc" in rest else "local")
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(2)
    except sqlite3.OperationalError as e:  # "database is locked": a scan holds the write lock
        print(f"error: {e}; if a scan is running, try again when it finishes")
        sys.exit(2)
    print(f"wrote {out}")


def cmd_fix(rest):
    import fixes
    sub = rest[0] if rest else ""
    conn = require_db()
    try:
        if sub == "record":
            need = [parse_named_arg(rest, f"--{k}") for k in ("project", "cause", "note")]
            if not all(need):
                print("usage: whytokens fix record --project P --cause C --note TEXT [--date YYYY-MM-DD] [--file PATH]")
                sys.exit(2)
            try:
                fid = fixes.record_fix(conn, *need, when=parse_named_arg(rest, "--date"),
                                       file_path=parse_named_arg(rest, "--file"))
            except (ValueError, OSError) as e:
                print(f"error: {e}")
                sys.exit(2)
            print(f"recorded fix #{fid}; run `whytokens verify {fid}` once enough sessions follow it")
        elif sub == "list":
            for f in fixes.list_fixes(conn):
                print(f"#{f['id']} {f['fix_date']} {f['project']} / {f['cause']}: {f['note']}")
        elif sub in ("edit", "delete"):
            try:
                fid = int(rest[1])
                if sub == "delete":
                    fixes.delete_fix(conn, fid)
                    print(f"deleted fix #{fid}")
                else:
                    when, note = parse_named_arg(rest, "--date"), parse_named_arg(rest, "--note")
                    if when is None and note is None:
                        raise ValueError("give --date and/or --note")
                    fixes.update_fix(conn, fid, when, note)
                    print(f"updated fix #{fid}")
            except (IndexError, ValueError) as e:
                print(f"error: {e}" if not isinstance(e, IndexError) else f"usage: whytokens fix {sub} ID ...")
                sys.exit(2)
        else:
            print("usage: whytokens fix record|list|edit|delete ...")
            sys.exit(2)
    finally:
        conn.close()


def cmd_pricing(rest):
    import pricing_check
    if rest[:1] != ["check"]:
        print("usage: whytokens pricing check [--offline]")
        sys.exit(2)
    lines, ok = pricing_check.check(offline="--offline" in rest)
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def cmd_budget(rest, today=None):
    import budget
    if rest[:1] == ["--clear"]:
        budget.set_budget(None, DB_PATH)
        print("budget cleared")
        return
    if rest:
        try:
            budget.set_budget(float(rest[0]), DB_PATH)
        except ValueError:
            print("error: AMOUNT must be a finite number greater than 0 (usage: whytokens budget [AMOUNT|--clear])")
            sys.exit(2)
    m = budget.month_status(DB_PATH, today=today)
    print(f"Month {m['month']} (estimated at API list price)")
    print(f"  spent so far:        ${m['spent']:,.2f}")
    print(f"  projected month-end: ${m['projected']:,.2f}")
    if m["budget"] is None:
        print("  budget:              not set (set one with `whytokens budget AMOUNT`)")
    else:
        over = m["gap"] > 0
        print(f"  budget:              ${m['budget']:,.2f}")
        print(f"  gap:                 {'over' if over else 'under'} by ${abs(m['gap']):,.2f}")
        need = m["needed_daily"]
        print("  needed daily rate:   " + (f"${need:,.2f}/day for the {m['days_remaining']} days left"
                                         if need is not None else "none (no days left this month)"))
    print("  " + ("Projection = spent + trailing 7-day daily average x days remaining" if m["method"] == "trailing_7d"
                  else "Projection = spent + month-to-date daily average x days remaining (fewer than 7 days of data)"))


def cmd_reconcile(rest, today=None):
    import json
    import reconcile
    require_db().close()  # scan first: never save a reading against a DB that doesn't exist yet
    sid = parse_named_arg(rest, "--session")
    try:
        if sid:
            usage = parse_named_arg(rest, "--usage")
            r = reconcile.session_check(sid, None if usage is None else float(usage), DB_PATH)
        else:
            period = parse_named_arg(rest, "--period") or reconcile.current_period()
            if "--clear" in rest:
                reconcile.set_billed(period, None, db_path=DB_PATH)
            elif "--billed" in rest:
                reconcile.set_billed(period, float(parse_named_arg(rest, "--billed")),
                                     parse_named_arg(rest, "--as-of") or reconcile.default_as_of(period), DB_PATH)
            r = reconcile.report(period, DB_PATH, today=today)
    except ValueError as e:
        print(f"error: {e} (usage: whytokens reconcile [--period YYYY-MM] [--billed AMOUNT [--as-of ISO]|--clear]"
              " | --session ID [--usage AMOUNT])")
        sys.exit(2)
    if "--json" in rest:
        print(json.dumps(r, indent=2))
        return
    money = lambda v: "?" if v is None else f"${v:,.2f}"
    if sid:
        print(f"Session {sid}: whytokens {money(r['whytokens'])} over {r['turns']} turns "
              f"(last scanned turn {r['last_turn']}), /usage {money(r['usage'])}"
              + (" (from Claude Code's cost-state record)" if r["usage_source"] == "cost-state" else "")
              + f", delta {money(r['delta'])}")
        print(f"  layers: {layer_line(r['layers'])} ({r['layers']['web_searches']} web searches)")
        print(f"  of which estimated summaries {money(r['estimated'])}, Haiku 5x premium {money(r['haiku_5x'])}")
        if r["cc_flag"]:
            print("  [flag] whytokens and Claude Code's cost-state differ by more than 5% with no known cause")
        if r["cc_coverage"] == "partial":
            print("  Claude Code's cost-state record is partial (a later process restarted its counter): not compared")
        for m in r["models"]:
            print(f"    {m['model']:<28} Claude Code {money(m['cc_cost']):>10}  whytokens {money(m['wt_cost']):>10}  {m['label']}")
        if r["cc_coverage"] != "none":
            print("  cost-state is an estimate at list prices, not a bill; see the dashboard's Claude Code column for its limits")
        return
    print(f"Reconcile {r['period']} ({r['cut']}, {r['basis']})")
    print(f"  whytokens shown ({r['tz']} days):  {money(r['shown'])}")
    print("  billed:                " + ("not entered (whytokens reconcile --period %s --billed AMOUNT)" % r["period"]
                                       if r["billed"] is None else money(r["billed"]) +
                                       (f" as of {r['as_of']} (whytokens cut there too)" if r["as_of"] else "")))
    if r["gap"] is not None:
        print(f"  gap (shown - billed):  {money(r['gap'])}")
    for row in r["rows"] + [r["remainder"]]:
        n = "" if row.get("turns") is None else f"  ({row['turns']} rows)"
        print(f"    {money(row['usd']):>10}  {row['label']}{n}")
    i, c = r["info"], r["coverage"]
    print("  Not subtracted (billed calls with estimated counts):")
    print(f"    {money(i['advisor_estimates']['usd']):>10}  advisor calls with estimated tokens "
          f"({i['advisor_estimates']['turns']} rows); {i['raised_output_turns']} turns with estimated output")
    print(f"    {money(i['coordinator_lower_bound']['usd']):>10}  claude.ai Projects seen locally (partial)")
    print(f"  Coverage: earliest Claude data {c['earliest_claude'] or 'none'}; {c['label']}")
    if c["projects_first_seen_in_period"]:
        print(f"  {len(c['projects_first_seen_in_period'])} project(s) first seen this month "
              "(new, or older history cleaned up before the first scan)")


def cmd_verify(fix_id=None):
    import fixes
    conn = require_db()
    try:
        todo = [f for f in fixes.list_fixes(conn) if fix_id in (None, str(f["id"]))]
    finally:
        conn.close()
    if not todo:
        print("No recorded fixes. Record one with `whytokens fix record`.")
        return
    for f in todo:
        print(fixes.format_result(fixes.verify(DB_PATH, f)))


def cmd_llm(rest, ask=input, interactive=None):
    if not config.HAS_LLM:  # left out of public builds
        print("whytokens llm: model help is not part of this build")
        sys.exit(2)
    import llm
    if not rest:
        t = llm.totals(DB_PATH)
        print(f"LLM adapter: {t['calls']} calls, ${t['total_usd']:.4f} ({t['basis']})")
        return
    kind, args = rest[0], rest[1:]
    if kind not in llm.MODELS or len(args) != (2 if kind == "draft" else 1):
        print("usage: whytokens llm label|judge SESSION  |  whytokens llm draft SESSION CAUSE")
        sys.exit(2)
    try:
        p = llm.preview(kind, args[0], args[1] if kind == "draft" else None, DB_PATH)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(2)
    print(f"Would run: {' '.join(a if a else repr(a) for a in p['argv'])}")
    print(f"Estimated cost: ${p['estimate_usd']:.4f} ({p['estimate_basis']})")
    print("Exactly this goes on stdin:\n" + "-" * 60 + "\n" + p["stdin"] + "\n" + "-" * 60)
    if not (sys.stdin.isatty() if interactive is None else interactive):
        print("Not sent: confirming needs an interactive terminal.")
        sys.exit(1)
    if ask("Send exactly this? [y/N] ").strip().lower() != "y":
        print("Not sent.")
        return
    r = llm.run(p["confirm"], DB_PATH)
    if r.get("cost_usd") is not None:
        print(f"Cost: ${r['cost_usd']:.4f}  adapter total: ${r['total_usd']:.4f}")
    if not r["ok"]:
        print(f"error: {r['error']}")
        fb = (r.get("fallback") or {}).get("deterministic_fix")
        if fb:
            print(f"Deterministic fix: {fb}")
        sys.exit(1)
    res = r["result"]
    print(f"[model-generated, {r['model']}]")
    if kind == "label":
        print(f"{res['label']}  ({res['outcome']})")
    elif kind == "judge":
        print(f"Observation: {res['mechanical']} of {res['of']} sampled turns looked mechanical "
              f"({res['share']:.0%}). Not a saving; not in any total.")
    else:
        print("Draft for CLAUDE.md (not written anywhere; copy it if you want it):")
        print(res["diff"])


def cmd_shapes():
    import api
    import shape
    d = api.get_shapes(DB_PATH)
    if not d["rows"]:
        print("no sessions yet; run `whytokens scan` first")
        return
    print(shape.format_breakdown(d["rows"]))
    print(f"\n{d['outliers']} session{'s' if d['outliers'] != 1 else ''} cost {d['constants']['outlier_x']:g}x or more "
          "their peers (an observation, not part of any total)")


def cmd_project(rest):
    import api
    import budget
    if rest[:1] != ["tag"] or len(rest) < 2:
        print("usage: whytokens project tag PROJECT [TAG|--clear]")
        sys.exit(2)
    project, arg = rest[1], rest[2:]
    try:
        if arg:
            api.post_project_tag({"project": project, "tag": None if arg == ["--clear"] else " ".join(arg)}, DB_PATH)
        tag = budget.load_tags(DB_PATH).get(project)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)
    print(f"{project}: {tag}" if tag else f"{project}: no tag")


COMMANDS = {
    "scan": cmd_scan,
    "doctor": cmd_doctor,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "live": cmd_live,
    "waste": cmd_waste,
    "reread": cmd_reread,
    "fix": cmd_fix,
    "card": cmd_card,
    "verify": cmd_verify,
    "pricing": cmd_pricing,
    "budget": cmd_budget,
    "reconcile": cmd_reconcile,
    "shapes": cmd_shapes,
    "llm": cmd_llm,
    "project": cmd_project,
    "export": cmd_export,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag:
            # `--note --date 2026-10-01` must not store the note "--date"
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                print(f"error: {flag} needs a value")
                sys.exit(2)
            return args[i + 1]
    return None

def main():
        if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
            import version
            print(version.line())
            sys.exit(0)
        if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
            print(usage())
            # a mistyped command is an error to a script; asking for help is not
            sys.exit(0 if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h") else 2)

        command = sys.argv[1]
        rest = sys.argv[2:]
        projects_dir = parse_named_arg(rest, "--projects-dir")

        if command not in ("dashboard", "scan", "doctor", "live", "pricing") and DB_PATH.exists():
            require_db().close()  # a DB from an older version gets its additive columns before any command reads it

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
        elif command == "export":
            cmd_export(as_json="--json" in rest)
        elif command == "waste":
            cmd_waste(parse_named_arg(rest, "--session"))
        elif command == "reread":
            cmd_reread(rest)
        elif command == "fix":
            cmd_fix(rest)
        elif command == "pricing":
            cmd_pricing(rest)
        elif command == "budget":
            cmd_budget(rest)
        elif command == "reconcile":
            cmd_reconcile(rest)
        elif command == "card":
            cmd_card(rest)
        elif command == "project":
            cmd_project(rest)
        elif command == "llm":
            cmd_llm(rest)
        elif command == "verify":
            cmd_verify(rest[0] if rest else None)
        else:
            COMMANDS[command]()

if __name__ == "__main__":
    main()
