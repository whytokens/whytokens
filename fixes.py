"""Recorded fixes and ``verify``: did a (project, cause) waste share fall after the fix?

Compares attributed % of spend (waste findings / session cost, at API list rate) over
sessions that started before the fix date with sessions that started on or after it.
Evidence tier "demonstrated saving" is only claimed when the share fell materially on
at least MIN_SESSIONS sessions per side. Quality is not measured.
"""

import hashlib
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import pricing
import waste

MIN_SESSIONS = 10
# ponytail: fixed materiality gate (>= 1 percentage point and >= 20% relative); tune on real data
MIN_DELTA_PP, MIN_DELTA_REL = 0.01, 0.20
CAVEAT = ("Quality is not measured: a lower waste share can also mean less work or a different "
          "mix of work, not only a better habit.")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_fix(conn, project, cause, note, when=None, file_path=None):
    """Store a fix. ``when`` is YYYY-MM-DD (default today). Returns the new id."""
    if cause not in waste.CAUSES:
        raise ValueError(f"unknown cause {cause!r}; one of {', '.join(waste.CAUSES)}")
    when = when or date.today().isoformat()
    # Reject malformed, store canonical (compared as text). fromisoformat reads 20260901 natively.
    when = date.fromisoformat(when).isoformat()
    if not conn.execute("SELECT 1 FROM sessions WHERE project_name = ? LIMIT 1", (project,)).fetchone():
        raise ValueError(f"unknown project {project!r}")
    cur = conn.execute(
        "INSERT INTO fixes (fix_date, project, cause, note, file_hash, recorded_at) VALUES (?,?,?,?,?,?)",
        (when, project, cause, note or "", file_hash(file_path) if file_path else None,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


def record_fix_once(conn, project, cause, note):
    """record_fix unless this (project, cause, note) is already recorded. Returns (id, duplicate).
    The check and the insert share one write transaction, so concurrent callers record it once."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM fixes WHERE project = ? AND cause = ? AND note = ? ORDER BY id LIMIT 1",
                           (project, cause, note or "")).fetchone()
        if row:
            conn.rollback()
            return row[0], True
        return record_fix(conn, project, cause, note), False  # commits
    except BaseException:
        conn.rollback()
        raise


def list_fixes(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, fix_date, project, cause, note, file_hash, recorded_at FROM fixes ORDER BY id")]


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


def verify(db_path, fix):
    """Before/after attributed share for one recorded fix (a dict from list_fixes)."""
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        totals = _session_totals(conn)
        rows = conn.execute("""SELECT session_id, COALESCE(date(first_timestamp, 'localtime'), substr(first_timestamp, 1, 10)) FROM sessions
                               WHERE project_name = ? AND COALESCE(agent, 'claude') = 'claude'
                               AND first_timestamp IS NOT NULL""", (fix["project"],)).fetchall()
    finally:
        conn.close()
    cause_usd = {}
    for f in waste.report(db_path, min_share=0, min_usd=0):
        if f["cause"] == fix["cause"]:
            cause_usd[f["session_id"]] = f["usd"]
    before = _side([(s, totals.get(s, 0.0)) for s, d in rows if d < fix["fix_date"]], cause_usd)
    after = _side([(s, totals.get(s, 0.0)) for s, d in rows if d >= fix["fix_date"]], cause_usd)
    out = {"fix": fix, "before": before, "after": after, "caveat": CAVEAT, "saving_usd": 0.0,
           "basis": waste.BASIS["label"],
           "min_sessions": MIN_SESSIONS}
    if min(before["sessions"], after["sessions"]) < MIN_SESSIONS:
        out["status"] = "insufficient data"
        out["reason"] = (f"needs at least {MIN_SESSIONS} sessions on each side "
                         f"(before {before['sessions']}, after {after['sessions']})")
        return out
    delta = before["pct"] - after["pct"]
    material = abs(delta) >= MIN_DELTA_PP and abs(delta) >= MIN_DELTA_REL * before["pct"]
    out["status"] = "no material change" if not material else ("reduced" if delta > 0 else "increased")
    if out["status"] == "reduced":
        out["saving_usd"] = delta * after["usd"]  # after-period spend at the old share, minus the actual
    return out


def format_result(r):
    f, b, a = r["fix"], r["before"], r["after"]
    lines = [f"fix #{f['id']} {f['fix_date']} {f['project']} / {f['cause']}: {f['note']}"
             + (f" [file sha256 {f['file_hash'][:12]}]" if f.get("file_hash") else "")]
    for name, s in (("before", b), ("after", a)):
        lines.append(f"  {name:<6} {s['sessions']:>4} sessions  ${s['cause_usd']:.2f} of ${s['usd']:.2f}  = {s['pct'] * 100:.1f}%")
    lines.append(f"  {r['status']}" + (f": {r['reason']}" if r.get("reason") else ""))
    if r["status"] == "reduced":
        lines.append(f"  share fell {(b['pct'] - a['pct']) * 100:.1f} points; about ${r['saving_usd']:.2f} less over the after period ({r['basis']})")
    lines.append(f"  caveat: {r['caveat']}")
    return "\n".join(lines)
