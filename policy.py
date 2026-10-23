"""Data policy: what the profiler stores about tool calls.

Default is allowlisted metadata: tool name, input *size*, and a small per-tool field
set. Raw tool inputs (file contents, shell commands, pasted text) are stored only
when ``WHYTOKENS_STORE_RAW_INPUTS`` is truthy. Stdlib only; imported by the scanner
and the Codex source so every writer of ``turns.tool_calls`` routes through here.
"""

import json
import os
import sqlite3

RAW_ENV = "WHYTOKENS_STORE_RAW_INPUTS"
MIGRATION_KEY = "tool_calls_redacted_v1"

# tool name -> input keys kept verbatim. Everything else is reduced to sizes.
_KEEP = {
    "Read": ("file_path",), "Write": ("file_path",), "Edit": ("file_path",),
    "MultiEdit": ("file_path",), "NotebookEdit": ("notebook_path",),
    "Task": ("subagent_type",), "Agent": ("subagent_type",), "Skill": ("skill",),
}
_COMMAND_TOOLS = ("Bash", "exec", "shell", "apply_patch")
_MAX_STR = 500


def store_raw_inputs():
    return os.environ.get(RAW_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def redact_call(call):
    """One ``{name, input}`` call reduced to allowlisted metadata. Idempotent."""
    if not isinstance(call, dict):
        return call
    name = call.get("name")
    inp = call.get("input")
    if not isinstance(inp, dict):
        inp = {}
    out = {}
    size = inp.get("input_size")
    if not isinstance(size, int) or isinstance(size, bool):
        size = len(json.dumps(inp, ensure_ascii=False, default=str))
    out["input_size"] = size
    for key in _KEEP.get(name, ()):
        v = inp.get(key)
        if isinstance(v, str):
            out[key] = v[:_MAX_STR]
    if name in _COMMAND_TOOLS:
        n = inp.get("command_len")
        if not isinstance(n, int) or isinstance(n, bool):
            cmd = inp.get("command")
            n = len(cmd) if isinstance(cmd, str) else None
        if n is not None:
            out["command_len"] = n
    return {"name": name, "input": out}


def redact_calls(calls):
    """The list form, unless the raw-input opt-in is on."""
    if store_raw_inputs():
        return calls
    return [redact_call(c) for c in calls]


def migrate_tool_calls(conn, meta_get, meta_set):
    """Rewrite stored ``turns.tool_calls`` under the default policy, once.

    Skipped (and not marked) while the raw opt-in is on. VACUUMs afterwards:
    without it the raw text stays recoverable in the file's free pages.
    """
    if store_raw_inputs() or meta_get(conn, MIGRATION_KEY):
        return 0
    rows = conn.execute(
        "SELECT rowid, tool_calls FROM turns WHERE tool_calls IS NOT NULL").fetchall()
    updates = []
    for rowid, text in rows:
        try:
            calls = json.loads(text)
        except (TypeError, ValueError):
            calls = None
        new = json.dumps([redact_call(c) for c in calls]) if isinstance(calls, list) else None
        if new != text:
            updates.append((new, rowid))
    conn.executemany("UPDATE turns SET tool_calls = ? WHERE rowid = ?", updates)
    conn.commit()
    # Purge before marking done: a blocked VACUUM/checkpoint (another connection open)
    # leaves the marker unset, so the next scan retries the purge.
    try:
        conn.execute("VACUUM")
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    except sqlite3.OperationalError:
        return len(updates)
    if not busy:
        meta_set(conn, MIGRATION_KEY, "1")
        conn.commit()
    return len(updates)
