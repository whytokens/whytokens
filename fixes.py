"""Recorded fixes and ``verify``: did a (project, cause) waste share fall after the fix?

Compares attributed % of spend (waste findings / session cost, at API list rate) over
sessions that started before the fix date with sessions that started on or after it.
Evidence tier "demonstrated saving" is only claimed when the share fell materially on
at least MIN_SESSIONS sessions per side. Quality is not measured.
"""

import hashlib
import sqlite3
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pricing
import shape
import waste

MIN_SESSIONS = 10
BASELINE_DAYS = (3, 5, 7)  # short before-windows the verdict is cross-checked against
MIN_WINDOW_SESSIONS = 5    # a short window under this is shown as skipped and never vetoes
# ponytail: fixed materiality gate (>= 1 percentage point and >= 20% relative); tune on real data
MIN_DELTA_PP, MIN_DELTA_REL = 0.01, 0.20
CAVEAT = ("Quality is not measured: a lower waste share can also mean less work or a different "
          "mix of work, not only a better habit.")


# Human labels for waste.CAUSES, served to the Changes tab (the Waste tab keeps its own copy in JS).
CAUSE_LABELS = {
    "resident_output": "Tool output kept in context", "repeated_reads": "Repeated file reads",
    "subagent_preamble": "Subagent preambles", "idle_gap": "Idle gaps (cache expiry)",
    "uncached_input": "Uncached input", "cache_miss_rebuild": "Unexpected cache misses",
    "deferred_tool_load": "Deferred tool schemas kept in context",
    "large_context": "Context carried past 250K", "retry_loop": "Retry loops", "fixed_prefix": "Fixed prefix re-read",
}
RATE_DAYS = 28  # window for a project's recent session pace


def _canon_date(when, today=None):
    """YYYY-MM-DD, canonical; malformed or future dates raise ValueError."""
    when = date.fromisoformat(when).isoformat()  # fromisoformat reads 20260901 natively
    if when > (today or date.today()).isoformat():
        raise ValueError(f"date {when} is in the future")
    return when


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_fix(conn, project, cause, note, when=None, file_path=None):
    """Store a fix. ``when`` is YYYY-MM-DD (default today). Returns the new id."""
    if cause not in waste.CAUSES:
        raise ValueError(f"unknown cause {cause!r}; one of {', '.join(waste.CAUSES)}")
    when = _canon_date(when or date.today().isoformat())
    if not conn.execute("SELECT 1 FROM sessions WHERE project_name = ? LIMIT 1", (project,)).fetchone():
        raise ValueError(f"unknown project {project!r}")
    cur = conn.execute(
        "INSERT INTO fixes (fix_date, project, cause, note, file_hash, recorded_at) VALUES (?,?,?,?,?,?)",
        (when, project, cause, note or "", file_hash(file_path) if file_path else None,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


def record_fix_once(conn, project, cause, note, when=None):
    """record_fix unless this (project, cause, note) is already recorded. Returns (id, duplicate).
    The check and the insert share one write transaction, so concurrent callers record it once."""
    conn.commit()
    if when is not None:
        _canon_date(when)  # a bad or future date is an error even when the change already exists
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM fixes WHERE project = ? AND cause = ? AND note = ? ORDER BY id LIMIT 1",
                           (project, cause, note or "")).fetchone()
        if row:
            conn.rollback()
            return row[0], True
        return record_fix(conn, project, cause, note, when), False  # commits
    except BaseException:
        conn.rollback()
        raise


def list_fixes(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, fix_date, project, cause, note, file_hash, recorded_at FROM fixes ORDER BY id")]


def update_fix(conn, fid, when=None, note=None):
    """Change a fix's date and/or note. The note stays required and (project, cause, note) stays unique.
    Read, duplicate check and write share one write transaction, so concurrent edits cannot both pass."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _update_fix(conn, fid, when, note)
    except BaseException:
        conn.rollback()
        raise


def _update_fix(conn, fid, when, note):
    row = conn.execute("SELECT project, cause, fix_date, note FROM fixes WHERE id = ?", (fid,)).fetchone()
    if not row:
        raise ValueError(f"no fix #{fid}")
    project, cause, old_when, old_note = row
    when = _canon_date(when) if when is not None else old_when
    note = old_note if note is None else note.strip()
    if not note:
        raise ValueError("note is required")
    if conn.execute("SELECT 1 FROM fixes WHERE project = ? AND cause = ? AND note = ? AND id != ?",
                    (project, cause, note, fid)).fetchone():
        raise ValueError("another change already has this project, cause and note")
    conn.execute("UPDATE fixes SET fix_date = ?, note = ? WHERE id = ?", (when, note, fid))
    conn.commit()


def delete_fix(conn, fid):
    if not conn.execute("DELETE FROM fixes WHERE id = ?", (fid,)).rowcount:
        raise ValueError(f"no fix #{fid}")
    conn.commit()


def _session_totals(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    totals = {}
    for t in conn.execute(f"""SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens,
                                     cache_creation_tokens, cache_creation_1h_tokens, timestamp,
                                     {pricing.price_mult_sql(cols)} AS mult FROM turns"""):
        totals[t[0]] = totals.get(t[0], 0.0) + pricing.calc_cost(
            t[1], t[2] or 0, t[3] or 0, t[4] or 0, t[5] or 0, t[7], t[6] or 0) * t[8]
    return totals


def _side(sessions, cause_usd):
    sessions = [(s, u) for s, u in sessions if u > 0]  # a zero-spend session has no share to measure
    usd = sum(u for _, u in sessions)
    cause = sum(cause_usd.get(s, 0.0) for s, _ in sessions)
    return {"sessions": len(sessions), "usd": usd, "cause_usd": cause, "pct": cause / usd if usd else 0.0}


def _counter_rows(conn):
    """Per session: (prompts, compactions, cache-read tokens, context tokens read or written)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    # advisor and agent-summary rows are synthetic copies that inherit a source turn's flag (waste._synthetic):
    # counting them would double a compaction
    bare_advisor = ("OR (COALESCE(message_id, '') = '' AND tool_name = 'advisor' AND COALESCE(tool_calls, '') = '')"
                    if {"tool_name", "tool_calls"} <= cols else "")
    comp = (f"""SUM(CASE WHEN message_id LIKE '{waste.ADVISOR_ID_PREFIX}%' OR message_id LIKE '{waste.AGENT_SUMMARY_ID_PREFIX}%'
                        {bare_advisor} THEN 0 ELSE COALESCE(is_compact_summary, 0) END)"""
            if "is_compact_summary" in cols else "0")
    prompts = "MAX(prompt_seq)" if "prompt_seq" in cols else "NULL"
    return {r[0]: r[1:] for r in conn.execute(
        f"""SELECT session_id, COALESCE({prompts}, 0), {comp}, SUM(COALESCE(cache_read_tokens, 0)),
                   SUM(COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0))
            FROM turns GROUP BY session_id""")}


def _gather_all(db_path, wait=30):
    """Everything verify measures, once for the whole DB: per-session spend, every claude session
    (project, id, start day, shape), each cause's attributed dollars per session, the counters and
    the sessions where each cause could not be measured. The spend and the waste report are separate
    reads, so a scan committing between them would pair two database states: data_version changes
    when another connection commits, so gather, check nothing committed meanwhile, and retry if it did."""
    watch = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        for _ in range(max(1, int(wait))):
            before = watch.execute("PRAGMA data_version").fetchone()[0]
            out = _gather_once(db_path)
            if watch.execute("PRAGMA data_version").fetchone()[0] == before:
                return out
            time.sleep(1)
    finally:
        watch.close()
    raise ValueError("the database kept changing while verify read it (a scan is running); try again when it finishes")


def _gather_once(db_path):
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")  # one snapshot: a scan landing between the reads must not pair old spend with new counts
        totals = _session_totals(conn)
        counters = _counter_rows(conn)
        shape_col = "shape" if "shape" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")} else "NULL"
        rows = conn.execute(f"""SELECT project_name, session_id, COALESCE(date(first_timestamp, 'localtime'), substr(first_timestamp, 1, 10)), {shape_col} FROM sessions
                               WHERE COALESCE(agent, 'claude') = 'claude'
                               AND first_timestamp IS NOT NULL""").fetchall()
    finally:
        conn.close()
    cause_usd, unmeasured = {}, {}
    for f in waste.report(db_path, min_share=0, min_usd=0, unmeasured=unmeasured):
        cause_usd.setdefault(f["cause"], {})[f["session_id"]] = f["usd"]
    return totals, rows, cause_usd, counters, unmeasured


def _slice(everything, fix):
    """One fix's cohort. A session where the cause could not be measured (its evidence is missing) is
    set aside, never counted as a zero share: lost evidence must not read as a saving. Every consumer
    (verdict, baselines, counters, shape mix) sees the same measured sessions; the set-aside ones are
    returned last so verify can count them."""
    totals, rows, cause_usd, counters, *rest = everything
    lost = rest[0] if rest else {}
    mine = [r[1:] for r in rows if r[0] == fix["project"]]
    gone = [r for r in mine if fix["cause"] in lost.get(r[0], ())]
    return totals, [r for r in mine if fix["cause"] not in lost.get(r[0], ())], cause_usd.get(fix["cause"], {}), counters, gone


def _gather(db_path, fix):
    """What verify measures for one fix: per-session spend, the project's sessions (id, start day, shape)
    and the cause's attributed dollars per session."""
    return _slice(_gather_all(db_path), fix)


def verify(db_path, fix, data=None):
    """Before/after attributed share for one recorded fix (a dict from list_fixes)."""
    totals, rows, cause_usd, *rest = data or _gather(db_path, fix)
    counters = rest[0] if rest else {}
    gone = rest[1] if len(rest) > 1 else []
    before = _side([(s, totals.get(s, 0.0)) for s, d, _ in rows if d < fix["fix_date"]], cause_usd)
    after = _side([(s, totals.get(s, 0.0)) for s, d, _ in rows if d >= fix["fix_date"]], cause_usd)
    # spending sessions set aside because the cause could not be measured in them
    before["unmeasured"] = sum(1 for s, d, _ in gone if d < fix["fix_date"] and totals.get(s, 0.0) > 0)
    after["unmeasured"] = sum(1 for s, d, _ in gone if d >= fix["fix_date"] and totals.get(s, 0.0) > 0)
    out = {"fix": fix, "before": before, "after": after, "caveat": CAVEAT, "saving_usd": 0.0,
           "basis": waste.BASIS["label"],
           "min_sessions": MIN_SESSIONS}
    if min(before["sessions"], after["sessions"]) < MIN_SESSIONS:
        out["status"] = "insufficient data"
        out["reason"] = (f"needs at least {MIN_SESSIONS} sessions on each side "
                         f"(before {before['sessions']}, after {after['sessions']})")
        if before["unmeasured"] or after["unmeasured"]:
            out["reason"] += (f"; {before['unmeasured'] + after['unmeasured']} more could not be measured "
                              f"(before {before['unmeasured']}, after {after['unmeasured']}): their tool results "
                              "or error flags were not recorded")
        return out
    delta = before["pct"] - after["pct"]
    # rounded for the floor only: 0.03 - 0.02 is a hair under 0.01 in floats
    material = round(abs(delta), 9) >= MIN_DELTA_PP and abs(delta) >= MIN_DELTA_REL * before["pct"]
    out["status"] = "no material change" if not material else ("reduced" if delta > 0 else "increased")
    if material and (before["unmeasured"] or after["unmeasured"]):
        # the set-aside sessions could hold exactly the waste that moved: a verdict needs every session measured
        out["status"] = "insufficient data"
        out["reason"] = (f"the share moved, but {before['unmeasured'] + after['unmeasured']} sessions could not be "
                         f"measured (before {before['unmeasured']}, after {after['unmeasured']}): their tool results "
                         "or error flags were not recorded, so no verdict is given")
        return out
    if fix["cause"] in waste.UNCOUNTED:  # an observation is never priced: share comparison only
        out["saving_usd"] = None
        out["note"] = f"{CAUSE_LABELS.get(fix['cause'], fix['cause'])} is a baseline cost, left out of the overhead total: the share is compared, no dollar figure."
    elif out["status"] == "reduced":
        out["saving_usd"] = delta * after["usd"]  # after-period spend at the old share, minus the actual
    out["counters"] = _counters(totals, rows, counters, fix)
    if out["status"] != "no material change":
        out["baselines"] = _baselines(totals, rows, cause_usd, fix, after, delta)
    return out


COUNTER_LABELS = {"usd_per_prompt": "$ per prompt", "prompts_per_session": "Prompts per session",
                  "compactions_per_session": "Compactions per session", "reread_share": "Re-read share of context"}


def _counters(totals, rows, counters, fix):
    """What else moved around the change, same project and the same spending sessions as the verdict.
    Observations only: every metric is listed, whichever way it moved, and none feeds the verdict.
    $ per prompt and prompts per session use sessions that have a prompt count; re-read share is
    cache-read tokens over all context tokens (input + cache read + cache write)."""
    def side(pick):
        ss = [s for s, d, _ in rows if totals.get(s, 0.0) > 0 and pick(d)]
        c = [counters[s] for s in ss if s in counters]
        asked = [(s, counters[s]) for s in ss if s in counters and counters[s][0] > 0]
        prompts = sum(x[1][0] for x in asked)
        ctx = sum(x[3] for x in c)
        return {"sessions": len(ss),
                "usd_per_prompt": sum(totals[s] for s, _ in asked) / prompts if prompts else None,
                "prompts_per_session": prompts / len(asked) if asked else None,
                "compactions_per_session": sum(x[1] for x in c) / len(ss) if ss else None,
                "reread_share": sum(x[2] for x in c) / ctx if ctx else None}
    b, a = side(lambda d: d < fix["fix_date"]), side(lambda d: d >= fix["fix_date"])
    out = []
    for k, label in COUNTER_LABELS.items():
        move = None if b[k] is None or a[k] is None else a[k] - b[k]
        out.append({"id": k, "label": label, "before": b[k], "after": a[k],
                    "direction": None if move is None else "up" if move > 1e-9 else "down" if move < -1e-9 else "flat"})
    return out


def _baselines(totals, rows, cause_usd, fix, after, delta):
    """Cross-check the full-baseline direction against the last 3/5/7 days before the fix. A window flips
    when its own before-vs-after move is at least MIN_DELTA_PP the other way. A window under
    MIN_WINDOW_SESSIONS is listed as skipped and cannot flip the result."""
    fix_day = date.fromisoformat(fix["fix_date"])
    wins = []
    for n in BASELINE_DAYS:
        start = (fix_day - timedelta(days=n)).isoformat()
        side = _side([(s, totals.get(s, 0.0)) for s, d, _ in rows if start <= d < fix["fix_date"]], cause_usd)
        w = {"days": n, "sessions": side["sessions"], "pct": side["pct"]}
        if side["sessions"] < MIN_WINDOW_SESSIONS:
            w["verdict"] = "skipped"
            w["reason"] = f"{side['sessions']} sessions, needs {MIN_WINDOW_SESSIONS}"
        else:
            d = side["pct"] - after["pct"]
            w["verdict"] = "disagrees" if round(abs(d), 9) >= MIN_DELTA_PP and (d > 0) != (delta > 0) else "agrees"
        wins.append(w)
    return {"disagree": any(w["verdict"] == "disagrees" for w in wins), "min_window_sessions": MIN_WINDOW_SESSIONS,
            "windows": wins}


def _recent_rate(db_path, project, today):
    """Sessions per day over the last RATE_DAYS days in this project (claude agent)."""
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        since = (today - timedelta(days=RATE_DAYS)).isoformat()
        n = conn.execute("""SELECT COUNT(*) FROM sessions WHERE project_name = ? AND COALESCE(agent, 'claude') = 'claude'
                            AND first_timestamp IS NOT NULL
                            AND COALESCE(date(first_timestamp, 'localtime'), substr(first_timestamp, 1, 10)) >= ?""",
                         (project, since)).fetchone()[0]
    finally:
        conn.close()
    return n / RATE_DAYS


def _mix(data, fix):
    """Shape mix on each side of the fix (share of spending sessions per shape), a warning when the work
    changed, and a same-shape before/after for every shape with MIN_SESSIONS on both sides. Informational:
    verify's verdict is computed without it."""
    totals, rows, cause_usd = data[:3]
    spend = [(s, d, sh) for s, d, sh in rows if totals.get(s, 0.0) > 0]
    side = {"before": [r for r in spend if r[1] < fix["fix_date"]], "after": [r for r in spend if r[1] >= fix["fix_date"]]}
    count = {k: {} for k in side}
    for k, rs in side.items():
        for _, _, sh in rs:
            count[k][sh or "unknown"] = count[k].get(sh or "unknown", 0) + 1
    n = {k: len(rs) for k, rs in side.items()}
    shapes = [sh for sh in (*shape.CATEGORIES, "mixed", "chat", "unknown") if sh in count["before"] or sh in count["after"]]
    share = {k: {sh: count[k].get(sh, 0) / n[k] for sh in shapes} if n[k] else {} for k in side}
    out = {"before": [{"shape": sh, "label": shape.label(sh), "pct": share["before"].get(sh, 0.0)} for sh in shapes] if n["before"] else [],
           "after": [{"shape": sh, "label": shape.label(sh), "pct": share["after"].get(sh, 0.0)} for sh in shapes] if n["after"] else [],
           "warn": False, "warning": "", "same_shape": []}
    if min(n.values()) >= MIN_SESSIONS:
        shift = {sh: round(abs(share["after"][sh] - share["before"][sh]), 9) for sh in shapes}
        top = max(shapes, key=lambda sh: shift[sh])
        if shift[top] >= shape.MIX_SHIFT:
            out["warn"] = True
            out["warning"] = (f"the work changed: {shape.label(top)} {share['before'][top] * 100:.0f}% -> "
                              f"{share['after'][top] * 100:.0f}% of sessions; read this verdict with care")
        for sh in shapes:
            sides = {k: _side([(s, totals.get(s, 0.0)) for s, _, x in rs if (x or "unknown") == sh], cause_usd) for k, rs in side.items()}
            if min(sides["before"]["sessions"], sides["after"]["sessions"]) >= MIN_SESSIONS:
                out["same_shape"].append({"shape": sh, "label": shape.label(sh), **sides})
    return out


def change_view(db_path, fix, today=None, everything=None):
    """verify() plus what the Changes tab shows. ``state`` is one of waiting / no_history / reduced /
    no_change / increased. A verdict state needs MIN_SESSIONS on both sides (verify's rule, unchanged);
    otherwise ``interim`` is true and the numbers are a preview, never a result."""
    today = today or date.today()
    data = _slice(everything, fix) if everything else _gather(db_path, fix)
    r = verify(db_path, fix, data)
    r["mix"] = _mix(data, fix)
    b, a = r["before"]["sessions"], r["after"]["sessions"]
    r["interim"] = r["status"] == "insufficient data"
    r["eta_days"] = None
    if b < MIN_SESSIONS:
        r["state"] = "no_history"
    elif a < MIN_SESSIONS or r["status"] == "insufficient data":  # enough sessions, but some were not measured
        r["state"] = "waiting"
        rate = _recent_rate(db_path, fix["project"], today)
        r["eta_days"] = math.ceil((MIN_SESSIONS - a) / rate) if rate > 0 and a < MIN_SESSIONS else None
    else:
        r["state"] = {"reduced": "reduced", "no material change": "no_change", "increased": "increased"}[r["status"]]
    r["cause_label"] = CAUSE_LABELS.get(fix["cause"], fix["cause"])
    return r


def format_result(r):
    f, b, a = r["fix"], r["before"], r["after"]
    lines = [f"fix #{f['id']} {f['fix_date']} {f['project']} / {f['cause']}: {f['note']}"
             + (f" [file sha256 {f['file_hash'][:12]}]" if f.get("file_hash") else "")]
    for name, s in (("before", b), ("after", a)):
        lines.append(f"  {name:<6} {s['sessions']:>4} sessions  ${s['cause_usd']:.2f} of ${s['usd']:.2f}  = {s['pct'] * 100:.1f}%"
                     + (f"  ({s['unmeasured']} not measurable, left out)" if s.get("unmeasured") else ""))
    lines.append(f"  {r['status']}" + (f": {r['reason']}" if r.get("reason") else ""))
    bl = r.get("baselines")
    if bl:
        lines.append("  baselines disagree: a shorter before-window points the other way" if bl["disagree"]
                     else "  baselines agree" if any(w["verdict"] != "skipped" for w in bl["windows"])
                     else "  no baseline window had enough sessions to check")
        for w in bl["windows"]:
            lines.append(f"    last {w['days']}d before: " + (f"skipped ({w['reason']})" if w["verdict"] == "skipped"
                         else f"{w['sessions']} sessions, {w['pct'] * 100:.1f}% -> {w['verdict']}"))
    if r["status"] == "reduced" and r["saving_usd"] is None:
        lines.append(f"  share fell {(b['pct'] - a['pct']) * 100:.1f} points; {r['note']}")
    elif r["status"] == "reduced":
        lines.append(f"  share fell {(b['pct'] - a['pct']) * 100:.1f} points; about ${r['saving_usd']:.2f} less over the after period ({r['basis']})")
    if r.get("counters") and r["status"] != "insufficient data":
        fmt = lambda c, v: "n/a" if v is None else (f"{v * 100:.1f}%" if c["id"] == "reread_share" else
                                                     f"${v:.2f}" if c["id"] == "usd_per_prompt" else f"{v:.2f}")
        lines.append("  also moved (observations, not a verdict):")
        for c in r["counters"]:
            lines.append(f"    {c['label']}: {fmt(c, c['before'])} -> {fmt(c, c['after'])}" + (f" ({c['direction']})" if c["direction"] else ""))
    lines.append(f"  caveat: {r['caveat']}")
    return "\n".join(lines)
