"""Guard the guard: break each rule of large_context, retry_loop and fixed_prefix in a temp copy of
the repo, one at a time, and confirm tests/test_context_retry_prefix.py goes red for every one.

    python scripts/falsify_context_retry_prefix.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # large_context: threshold ignored (every turn counts, or every token of a big turn)
    ("waste.py", "        if ctx > LARGE_CONTEXT_TOKENS:", "        if ctx > 0:"),
    ("waste.py", "            tokens = ctx - LARGE_CONTEXT_TOKENS", "            tokens = ctx"),
    # large_context: compaction does not reset the crossing
    ("waste.py", '        if t["is_compact_summary"]:\n            over = False', "        if False:\n            over = False"),
    # retry_loop: the first attempt is priced
    ("waste.py", 'if t["id"] in priced or t["id"] == run[0]["id"]:', 'if t["id"] in priced:'),
    # retry_loop: an intervening Edit/Write is ignored (an edit-test loop flagged)
    ("waste.py", 'elif err == 1 or call.get("name") in _WRITERS:', "elif err == 1:"),
    # retry_loop: a writer only tool_results knows about (streamed message) ignored; unknown prompt boundary trusted
    ("waste.py", 'for cid, name in trcalls.get(t["message_id"], ())):', 'for cid, name in ()):'),
    ("waste.py", 'if t["prompt_seq"] is None or any(', 'if any('),
    # retry_loop: two failures already a loop
    ("waste.py", "if st and len(st[2]) >= RETRY_RUN:", "if st and len(st[2]) >= 2:"),
    # retry_loop inputs: error flag never stored, command not whitespace-normalised
    ("scanner.py", '1 if b.get("is_error") is True else 0', "0"),
    ("policy.py", 'hashlib.sha256(" ".join(cmd.split()).encode("utf-8"))', 'hashlib.sha256(cmd.encode("utf-8"))'),
    # backfill: overwrites a value already there
    ("sources/claude.py", "SET is_error = ? WHERE call_id = ? AND is_error IS NULL", "SET is_error = ? WHERE call_id = ?"),
    ("sources/claude.py", 'c.get("name") == "Bash" and "command_hash" not in inp', 'c.get("name") == "Bash"'),
    # backfill: a session whose transcript is gone treated as success (0 = "no error")
    ("sources/claude.py", "        if complete:\n            scanner._meta_set(conn, marker, \"1\")\n        conn.commit()\n        return filled",
     "        conn.execute(\"UPDATE tool_results SET is_error = 0 WHERE is_error IS NULL\")\n"
     "        if complete:\n            scanner._meta_set(conn, marker, \"1\")\n        conn.commit()\n        return filled"),
    # backfill: touches a cost column while rewriting tool_calls
    ("sources/claude.py", 'conn.execute("UPDATE turns SET tool_calls = ? WHERE message_id = ?", (json.dumps(stored), mid))',
     'conn.execute("UPDATE turns SET tool_calls = ?, output_tokens = output_tokens + 1 WHERE message_id = ?", (json.dumps(stored), mid))'),
    # backfill never wired into the scan
    ("sources/__init__.py", "            source.backfill_retry_inputs(conn)", "            pass"),
    # measurement: unmeasured sessions not counted
    ("waste.py", "    if measured == total:\n", "    if True:\n"),
    # observations summed into the totals
    ("waste.py", '"in_total": cause not in UNCOUNTED})', '"in_total": True})'),
    ("api.py", 'sum(f["usd"] for f in found if f.get("in_total", True))', 'sum(f["usd"] for f in found)'),
    ("api.py", 'sum(f["usd"] for f in findings if f.get("in_total", True))', 'sum(f["usd"] for f in findings)'),
    ("static/modules/drill.js", "if (counted(f)) e.total += f.usd;", "e.total += f.usd;"),
    ("static/modules/app.js", "const total = inScope.filter(counted).reduce(", "const total = inScope.reduce("),
    ("static/modules/app.js", "v.causes.filter(c => c.in_total).slice(0, 3)", "v.causes.slice(0, 3)"),
    # fixed_prefix: a subagent lane counted as main lane
    ("waste.py", 'not t["is_sidechain"] and not _synthetic(t)]', "not _synthetic(t)]"),
    # fixed_prefix: lean prefix still reported; later turns not capped at the prefix
    ("waste.py", "    if prefix < FIXED_PREFIX_TOKENS:\n        return", "    if False:\n        return"),
    ("waste.py", 'tokens = min(prefix, t["cache_read_tokens"] or 0)', 'tokens = t["cache_read_tokens"] or 0'),
]
TESTS = ["tests.test_context_retry_prefix"]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.db", "build", "*.egg-info", ".claude", ".local-delegate"))
        if run(tree) != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig!r}")
                failures += 1
                continue
            path.write_bytes(src.replace(orig, mutant).encode("utf-8"))
            caught = run(tree) != 0
            path.write_bytes(src.encode("utf-8"))
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
