"""leakcheck.py - generic "this output leaks nothing from that database" check.

    leaked(db_path, text, extra=(), ignore=())  -> sorted list of leaked strings (empty = clean)
    assert_no_leak(testcase, db_path, text, ...)

Every text column of every table is suspect (tool_name carries MCP names, tool_calls carries commands,
extension tables carry free text) except the closed vocabularies and ids in SAFE (any table) and SAFE_PAIRS (named table and column). JSON values are
unpacked so strings inside them count. ``text`` is checked raw and HTML-unescaped, so a renderer that
encodes & < > cannot hide a leak. ``extra`` adds strings the DB does not hold (tool inputs read from a
transcript, say); ``ignore`` removes values that are legitimately printed. Values under MIN_LEN
characters are skipped: a 2-letter branch matches every sentence."""

import html
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

MIN_LEN = 3
SAFE = {"model", "stop_reason", "service_tier", "inference_geo", "agent", "effort", "status", "agent_type",
        "shape", "speed", "source", "topic_source", "session_id", "message_id", "call_id", "agent_id",
        "timestamp", "first_timestamp", "last_timestamp", "fix_date", "recorded_at", "completed_at"}
# key/value are generic names: only the core's own table is a closed vocabulary, never an extension's
SAFE_PAIRS = {("schema_meta", "key"), ("schema_meta", "value")}


def _strings(v, keys=False):
    """The value itself plus every string inside it when it is JSON (tool_calls holds a list of calls)."""
    out = [v]
    try:
        stack = [json.loads(v)]
    except ValueError:
        return out
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            stack.extend(x.keys() if keys else ())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


def _core_tables():
    """The tables the core itself creates: SAFE columns are exempt only there, never in an extension's table."""
    import scanner
    conn = sqlite3.connect(":memory:")
    scanner.init_db(conn)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def secrets(db_path, extra=()):
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    found = {s for s in extra if isinstance(s, str)}
    core = _core_tables()
    try:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for col in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
                if (col in SAFE and table in core) or (table, col) in SAFE_PAIRS:
                    continue
                for (v,) in conn.execute(f'SELECT DISTINCT "{col}" FROM "{table}"'):
                    if isinstance(v, str):
                        found.update(_strings(v))
        # schema_meta holds versions and markers; a path stored there is not one
        for (v,) in conn.execute("SELECT value FROM schema_meta WHERE value LIKE '%/%' OR value LIKE '%\%'")                 if "schema_meta" in core and conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone() else []:
            if isinstance(v, str):
                found.update(_strings(v, keys=True))
    finally:
        conn.close()
    return {s for s in found if len(s) >= MIN_LEN}


def leaked(db_path, text, extra=(), ignore=()):
    seen = (text, html.unescape(text))
    return sorted(s for s in secrets(db_path, extra) - set(ignore) if any(s in t for t in seen))


def assert_no_leak(case, db_path, text, extra=(), ignore=()):
    case.assertEqual(leaked(db_path, text, extra, ignore), [], "output contains strings from the database")
