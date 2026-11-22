"""Break each cost-state checksum rule one at a time and check tests/test_cost_state.py goes red.

Run: python scripts/falsify_cost_state.py   (exit 0 = every mutant caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTANTS = [
    # last record wins
    ("sources/claude.py", "                last = (n, r)", "                last = last or (n, r)"),
    # a new record with no new turns still updates
    ("sources/__init__.py", "if len(parsed) > 5 and parsed[5]:", "if turns and len(parsed) > 5 and parsed[5]:"),
    # backfill of an existing DB
    ("sources/claude.py", "                found = last_cost_state(filepath)", "                found = None"),
    # coverage slack, both directions
    # the first turn must be whole ms, or the inclusive coverage edge flips at random
    ("sources/claude.py", '"SELECT ROUND((julianday(timestamp) - 2440587.5) * 86400000.0) FROM turns"',
     '"SELECT (julianday(timestamp) - 2440587.5) * 86400000.0 FROM turns"'),
    ("cost_state.py", "SLACK_MS = 60_000 ", "SLACK_MS = 0 "),
    ("cost_state.py", "SLACK_MS = 60_000 ", "SLACK_MS = 120_000 "),
    # a record with no turns is not shown
    ("cost_state.py", "            continue  # no turns", "            pass  # no turns"),
    # partial is never compared
    ("cost_state.py", "            continue  # a later process", "            pass  # a later process"),
    # alias normalisation
    ("cost_state.py", 'return (model or "").split("[")[0]', 'return model or ""'),
    # reprice tolerance, both directions
    ("cost_state.py", "REPRICE_TOL = 0.01 ", "REPRICE_TOL = 0.03 "),
    ("cost_state.py", "REPRICE_TOL = 0.01 ", "REPRICE_TOL = 0.001 "),
    ("cost_state.py", '+ n("webSearchRequests") * pricing.WEB_SEARCH_FEE_USD)', ")"),
    # Haiku exception: present, and only with cache_read
    ("cost_state.py", 'if pricing.LONG_PROMPT_MODEL in model and n("cacheReadInputTokens"):', "if False:"),
    ("cost_state.py", 'if pricing.LONG_PROMPT_MODEL in model and n("cacheReadInputTokens"):',
     "if pricing.LONG_PROMPT_MODEL in model:"),
    # no 5m/1h split recorded
    ("cost_state.py", 'if n("cacheCreationInputTokens") and "cacheCreationEphemeral1hInputTokens" not in u:', "if False:"),
    # flag threshold, both directions, both signs, the dollar floor and the known-cause exemption
    ("cost_state.py", "FLAG_PCT = 0.05 ", "FLAG_PCT = 0.07 "),
    ("cost_state.py", "FLAG_PCT = 0.05 ", "FLAG_PCT = 0.03 "),
    ("cost_state.py", 'and abs(delta) > FLAG_PCT', 'and delta > FLAG_PCT'),
    ("cost_state.py", "FLAG_MIN_USD = 0.10 ", "FLAG_MIN_USD = 0.0 "),
    ("cost_state.py", "FLAG_MIN_USD = 0.10 ", "FLAG_MIN_USD = 0.2 "),
    ("cost_state.py", 'and not any(m["label"] == "priced_differently"', 'and not any(False'),
    # reconcile: fills only from a full record, and an entered figure wins
    ("reconcile.py", 'if usage is None and cc.get("cc_coverage") == "full":', 'if usage is None and cc.get("cc_cost") is not None:'),
    ("reconcile.py", 'if usage is None and cc.get("cc_coverage") == "full":', 'if cc.get("cc_coverage") == "full":'),
    # review round: earliest instant, late coverage, retryable backfill, malformed numbers, old DBs, export
    ("sources/claude.py", '" ORDER BY julianday(timestamp) LIMIT 1", (sid,))', '" ORDER BY timestamp LIMIT 1", (sid,))'),
    ("sources/claude.py", " AND timestamp LIKE '____-__-__%'", ""),
    ("sources/__init__.py", "            source.sync_cost_state(conn)", "            pass"),
    ("sources/claude.py", "            if failed:\n                conn.commit()\n                return 0\n", ""),
    ("cost_state.py", "    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and -1e18 < v < 1e18 else 0",
     "    return v"),
    # second review round
    ("sources/claude.py", "sid if isinstance(sid, str) and sid else", "sid if sid else"),
    ("sources/claude.py", " AND julianday(timestamp) IS NOT NULL\"", "\""),
    ("sources/claude.py", "SELECT 1 FROM processed_files WHERE COALESCE(source, 'claude') = 'claude'",
     "SELECT 1 FROM turns WHERE COALESCE(agent, 'claude') = 'claude'"),
    ("cost_state.py", "and abs(delta) > FLAG_PCT * (1 + 1e-12)", "and abs(delta) > FLAG_PCT"),
    ("cost_state.py", "and abs(delta) > FLAG_PCT * (1 + 1e-12)", "and abs(delta) > FLAG_PCT * (1 + 1e-6)"),
    ("cost_state.py", 'conn.execute("ROLLBACK")', "conn.rollback()"),
    ("sources/claude.py", 'start_time_ms, covers_full FROM cost_state")', 'start_time_ms, covers_full FROM cost_state WHERE covers_full IS NULL")'),
    ("api.py", 'if sort == "cc_cost" and not cost_state.has_table(conn):', "if False:"),
    ("api.py", "sessions = [_session_to_dict(r, by_model, cc=cc) for r in rows]",
     "sessions = [_session_to_dict(r, by_model) for r in rows]"),
    # surfaces
    ("api.py", '"cc_flag":         c.get("cc_flag", False),', '"cc_flag":         False,'),
]
TESTS = "tests.test_cost_state"


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", TESTS], cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__", "*.db", "docs"))
        if run(tree) != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig!r}")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            caught = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]} -> {mutant.strip()[:30]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
