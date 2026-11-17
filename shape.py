"""Work shape per session and peer baselines.

A session's shape comes from its tool-call mix alone (deterministic, no model, no transcript):
the dominant category when it holds at least DOMINANT_SHARE of the tool calls, else "mixed";
under MIN_TOOL_CALLS calls it is "chat". Shapes are stored on ``sessions`` with the rule version
that derived them, so a rule change re-derives from the DB.

A session is compared with its peers: the median cost per user prompt of the same project and
shape (itself included). Under MIN_PEERS the project's median across shapes is used and says so;
with neither there is no multiple. A multiple at OUTLIER_X or more is an observation: it is
counted and listed, never added to any cost or overhead total.

Image input is not recorded by ingestion, so it is not a category here.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from urllib.parse import quote

SHAPE_VERSION = 1
DOMINANT_SHARE = 0.40   # a category must hold at least this share of tool calls to name the shape
MIN_TOOL_CALLS = 5      # fewer calls than this and the session is chat
MIN_PEERS = 5           # peers needed (itself included) before a baseline is trusted
OUTLIER_X = 2.0        # multiple of the peer median that counts as an outlier
MIX_SHIFT = 0.20        # share-of-sessions change in any one shape that flags a changed mix

CATEGORIES = ("edit", "read_search", "shell", "web", "connector", "subagent")  # also the tie-break order
LABELS = {
    "edit": "edit-heavy", "read_search": "read/search-heavy", "shell": "shell-heavy",
    "web": "web-heavy", "connector": "connector-heavy", "subagent": "subagent-heavy",
    "mixed": "mixed", "chat": "chat",
}
_TOOLS = {
    "edit": {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"},
    "read_search": {"Read", "Grep", "Glob"},
    "shell": {"Bash", "PowerShell", "shell", "exec_command", "local_shell"},
    "web": {"WebFetch", "WebSearch"},
    "subagent": {"Agent", "Task"},
}
_CATEGORY_OF = {name: cat for cat, names in _TOOLS.items() for name in names}


def label(shape):
    return LABELS.get(shape, "not derived yet")


def category(tool_name):
    """(category or None, connector server or None) for one tool name."""
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        return "connector", (parts[1] if len(parts) > 2 and parts[1] else None)
    return _CATEGORY_OF.get(tool_name), None


def derive(names):
    """(shape, detail) from a session's tool names. detail is the top connector server, else None."""
    n = len(names)
    if n < MIN_TOOL_CALLS:
        return "chat", None
    cats, servers = Counter(), Counter()
    for name in names:
        cat, server = category(name)
        if cat:
            cats[cat] += 1
        if server:
            servers[server] += 1
    top = max(CATEGORIES, key=lambda c: cats[c])
    if cats[top] / n >= DOMINANT_SHARE:
        return top, (servers.most_common(1)[0][0] if top == "connector" and servers else None)
    return "mixed", None


def _names(blob):
    try:
        return [c["name"] for c in json.loads(blob) if isinstance(c, dict) and isinstance(c.get("name"), str)]
    except (TypeError, ValueError):
        return []


_SIG = ("SELECT s.session_id, s.turn_count + COALESCE(t.n, 0) FROM sessions s LEFT JOIN "
        "(SELECT session_id, SUM(COALESCE(LENGTH(tool_calls), 0)) n FROM turns GROUP BY session_id) t "
        "ON t.session_id = s.session_id")


def sync_shapes(conn):
    """Derive and store the shape of every session that has none, was derived by another rule
    version, or whose inputs moved since: shape_turns holds a signature of turn count plus stored
    tool-call bytes, so new turns and a tool_calls backfill both re-derive. Reads only the DB."""
    sig = dict(conn.execute(_SIG))
    stale = [sid for sid, shape, ver, st in conn.execute(
        "SELECT session_id, shape, shape_version, shape_turns FROM sessions")
        if shape is None or ver != SHAPE_VERSION or st != sig.get(sid)]
    if not stale:
        return 0
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _stale_shape (session_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _stale_shape")
    conn.executemany("INSERT INTO _stale_shape VALUES (?)", [(s,) for s in stale])
    calls = defaultdict(list)
    for sid, blob in conn.execute("SELECT t.session_id, t.tool_calls FROM turns t JOIN _stale_shape x "
                                  "ON x.session_id = t.session_id WHERE t.tool_calls IS NOT NULL"):
        calls[sid].extend(_names(blob))
    conn.executemany(
        "UPDATE sessions SET shape = ?, shape_detail = ?, shape_version = ?, shape_turns = ? WHERE session_id = ?",
        [(*derive(calls[s]), SHAPE_VERSION, sig.get(s), s) for s in stale])
    conn.execute("DROP TABLE _stale_shape")
    conn.commit()
    return len(stale)


def session_rows(db_path):
    """One dict per session: id, project, shape, detail, usd (pricing.py, via fixes), prompts (main thread)."""
    import fixes  # fixes imports this module; the cost path stays single
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        totals = fixes._session_totals(conn)
        prompts = dict(conn.execute("SELECT session_id, MAX(prompt_seq) FROM turns WHERE prompt_seq IS NOT NULL GROUP BY session_id"))
        return [{"session_id": sid, "project": proj or "unknown", "shape": shape, "detail": detail,
                 "usd": totals.get(sid, 0.0), "prompts": prompts.get(sid) or 0}
                for sid, proj, shape, detail in conn.execute("SELECT session_id, project_name, shape, shape_detail FROM sessions")]
    finally:
        conn.close()


def peers(rows):
    """{session_id: {x, basis, n, median, shape}} for sessions with a known shape, prompts and spend.
    x = cost per prompt / median cost per prompt of the peer group; basis is "shape" or "project";
    x is None when neither group reaches MIN_PEERS."""
    elig = [dict(r, cpp=r["usd"] / r["prompts"]) for r in rows if r["shape"] and r["prompts"] > 0 and r["usd"] > 0]
    by_shape, by_project = defaultdict(list), defaultdict(list)
    for r in elig:
        by_shape[(r["project"], r["shape"])].append(r["cpp"])
        by_project[r["project"]].append(r["cpp"])
    out = {}
    for r in elig:
        for basis, group in (("shape", by_shape[(r["project"], r["shape"])]), ("project", by_project[r["project"]])):
            if len(group) >= MIN_PEERS:
                m = median(group)
                out[r["session_id"]] = {"x": r["cpp"] / m, "basis": basis, "n": len(group), "median": m, "shape": r["shape"]}
                break
        else:
            out[r["session_id"]] = {"x": None, "basis": None, "n": len(by_shape[(r["project"], r["shape"])]),
                                    "median": None, "shape": r["shape"]}
    return out


def is_outlier(x):
    return x is not None and x >= OUTLIER_X


def baseline_text(p, project=""):
    """'2.4x the median for edit-heavy sessions in this project, n=18' (empty when no multiple)."""
    if not p or p["x"] is None:
        return ""
    where = f"in {project}" if project else "in this project"
    kind = f"{label(p['shape'])} sessions" if p["basis"] == "shape" else "sessions of any shape"
    return f"{p['x']:.1f}x the median for {kind} {where}, n={p['n']}"


def breakdown(rows, findings=()):
    """Per-shape table. Overhead counts only findings that are in the total (observation tier is
    shown elsewhere, never summed). Rows with a NULL shape are grouped as "unknown"."""
    over = defaultdict(float)
    for f in findings:
        if f.get("in_total", True):
            over[f["session_id"]] += f["usd"]
    acc = {}
    for r in rows:
        a = acc.setdefault(r["shape"] or "unknown", {"shape": r["shape"] or "unknown", "sessions": 0, "usd": 0.0, "overhead_usd": 0.0})
        a["sessions"] += 1
        a["usd"] += r["usd"]
        a["overhead_usd"] += over.get(r["session_id"], 0.0)
    total = sum(a["usd"] for a in acc.values())
    out = []
    for a in sorted(acc.values(), key=lambda a: -a["usd"]):
        a["label"] = label(a["shape"])
        a["share"] = a["usd"] / total if total else 0.0
        a["overhead_share"] = a["overhead_usd"] / a["usd"] if a["usd"] else 0.0
        out.append(a)
    return out


def format_breakdown(rows):
    """The same table as the dashboard, for the terminal."""
    lines = [f"{'work shape':<20}{'sessions':>9}{'cost':>12}{'share':>8}{'overhead':>10}"]
    for r in rows:
        lines.append(f"{r['label']:<20}{r['sessions']:>9}{'$' + format(r['usd'], ',.2f'):>12}"
                     f"{r['share'] * 100:>7.1f}%{r['overhead_share'] * 100:>9.1f}%")
    lines.append("overhead = attributed overhead as a share of that shape's cost (lower bound)")
    return "\n".join(lines)
