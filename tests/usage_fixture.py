"""A temp usage DB built row by row, for the budget and what-if tests."""

import tempfile
from pathlib import Path

from scanner import get_db, init_db

OPUS = "claude-opus-5-5"   # $20 / MTok output: 50_000 output tokens cost exactly $1.00


def make_db(turns, agents=(), project="acme"):
    """turns: dicts with day, model, session (default 's1') and any token / speed / geo / agent_id
    fields. Returns (db Path, tempdir)."""
    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "usage.db"
    conn = get_db(db)
    init_db(conn)
    for sid in {t.get("session", "s1") for t in turns}:
        conn.execute("INSERT INTO sessions (session_id, project_name) VALUES (?, ?)", (sid, project))
    for i, t in enumerate(turns):
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens, cache_read_tokens,"
            " cache_creation_tokens, cache_creation_1h_tokens, speed, inference_geo, agent_id, message_id, is_sidechain)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("session", "s1"), t["day"] + "T12:00:00Z", t.get("model", OPUS), t.get("input", 0),
             t.get("output", 0), t.get("cache_read", 0), t.get("cache_creation", 0), t.get("cc1h", 0),
             t.get("speed"), t.get("geo"), t.get("agent_id"), f"m{i}", t.get("sidechain", 0)))
    for agent_id, agent_type in agents:
        conn.execute("INSERT INTO agents (agent_id, agent_type) VALUES (?, ?)", (agent_id, agent_type))
    conn.commit()
    conn.close()
    return db, tmp


def november(days=range(1, 11)):
    """Day d of 2026-11 spends exactly $d on Opus 5.5 output."""
    return [{"day": f"2026-11-{d:02d}", "output": d * 50_000} for d in days]
