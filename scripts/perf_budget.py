"""Performance budget on a synthetic corpus: cold scan, warm scan, page load, waste.

    python scripts/perf_budget.py [--sessions N] [--turns N]

Exits 1 if any measure is over budget. The budget scales with corpus size, so the
unit test runs the same check on a small corpus (tests/test_first_run.py).
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Seconds per 100k turns (page/waste are fixed ceilings: they must not grow with corpus
# because the UI pages /api/sessions). Set from measured runs with ~3x headroom.
BUDGET_PER_100K_TURNS = {"cold_scan": 60.0, "warm_scan": 6.0}
BUDGET_FIXED = {"page_load": 5.0, "sessions_page": 0.5, "waste": 5.0}


def write_corpus(root, sessions, turns):
    """Claude-shaped transcripts: one project dir per 50 sessions, tool calls every 5th turn."""
    root = Path(root)
    for s in range(sessions):
        d = root / f"proj-{s // 50}"
        d.mkdir(parents=True, exist_ok=True)
        sid = f"sess-{s:06d}"
        lines = [json.dumps({"type": "user", "sessionId": sid, "cwd": f"/work/proj-{s // 50}",
                             "timestamp": "2026-04-08T09:00:00Z"})]
        for t in range(turns):
            content = [{"type": "tool_use", "id": f"tu-{s}-{t}", "name": "Read",
                        "input": {"file_path": f"/work/f{t}.py"}}] if t % 5 == 0 else []
            lines.append(json.dumps({
                "type": "assistant", "sessionId": sid, "cwd": f"/work/proj-{s // 50}",
                "timestamp": f"2026-04-{1 + s % 28:02d}T{t // 60 % 24:02d}:{t % 60:02d}:00Z",
                "message": {"id": f"msg-{s}-{t}", "model": "claude-sonnet-4-6", "stop_reason": "end_turn",
                            "usage": {"input_tokens": 100 + t, "output_tokens": 50,
                                      "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 200},
                            "content": content}}))
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def measure(sessions, turns):
    import api
    import scanner
    import sources
    from sources.claude import ClaudeSource

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_corpus(tmp / "p", sessions, turns)
        db = tmp / "perf.db"
        srcs = lambda: [ClaudeSource(projects_dirs=[tmp / "p"])]
        out = {}

        def timed(name, fn):
            t0 = time.perf_counter()
            fn()
            out[name] = time.perf_counter() - t0

        timed("cold_scan", lambda: sources.scan_all(db_path=db, sources=srcs(), extensions=[], verbose=False))
        timed("warm_scan", lambda: sources.scan_all(db_path=db, sources=srcs(), extensions=[], verbose=False))
        timed("page_load", lambda: api.get_dashboard_data(db_path=db))
        timed("sessions_page", lambda: api.get_sessions_page(limit=50, db_path=db))
        timed("waste", lambda: api.get_waste(db_path=db))
        out["turns"] = sessions * turns
        return out


def check(measured, scale=1.0):
    """Return a list of over-budget messages (empty = within budget). ``scale`` loosens
    every limit (the unit test passes >1 so a loaded CI box can't flake it)."""
    over = []
    per = measured["turns"] / 100_000
    for k, v in BUDGET_PER_100K_TURNS.items():
        limit = max(BUDGET_PER_100K_TURNS[k] * per, 1.0) * scale
        if measured[k] > limit:
            over.append(f"{k}: {measured[k]:.2f}s > {limit:.2f}s")
    for k, limit in BUDGET_FIXED.items():
        if measured[k] > limit * scale:
            over.append(f"{k}: {measured[k]:.2f}s > {limit * scale:.2f}s")
    return over


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=2000)
    ap.add_argument("--turns", type=int, default=100)
    a = ap.parse_args()
    m = measure(a.sessions, a.turns)
    print(json.dumps({k: round(v, 3) for k, v in m.items()}, indent=1))
    over = check(m)
    print("WITHIN BUDGET" if not over else "OVER BUDGET:\n  " + "\n  ".join(over))
    sys.exit(1 if over else 0)
