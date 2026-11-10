"""Guard the guard: break the tool_use_id dedupe, the per-lane scoping, the per-lane
call check, the reload and overlap rules in a temp copy of the repo, one at a time, and
confirm tests/test_deferred_tools.py goes red for every one.

    python scripts/falsify_deferred_tools.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # dedupe by tool_use_id: the same result appears twice and must not double its names
    ("scanner.py", 'list(dict.fromkeys(row.get("loaded_tools", []) + names))', 'row.get("loaded_tools", []) + names'),
    # subagent lanes counted as one -> a false "loaded twice"
    ("waste.py", 'lanes.setdefault(t["agent_id"] if t["is_sidechain"] else None, [])', "lanes.setdefault(None, [])"),
    # per-lane call check dropped -> unused is never flagged
    ("waste.py", "if last_call.get(n, -1) <= i]", "if False]"),
    ("waste.py", "if n in seen and not any(seen[n] < k <= i for k in compact_at)]", "if False]"),
    ("waste.py", "if n in dup_names]", "if False]"),
    ("waste.py", "dup_names, lane_pairs = _overlaps([loads[t[\"message_id\"]] for t in lt if", "dup_names, lane_pairs = _overlaps([loads[t[\"message_id\"]] for t in turns if"),
    # doctor: duplicates grouped by launch spec, plugin config read
    ("mcp_config.py", "if len(set(g)) > 1]", "if len(set(g)) > 5]"),
    ("mcp_config.py", "sorted(set(g)) for g", "g for g"),
    ("waste.py", "stop = min((k for k in compact_at if k > i), default=len(lt))", "stop = len(lt)"),
    # combined review: compaction on the very next turn, later turns counted one by one, retryable backfill
    ("waste.py", "stop = min((k for k in compact_at if k > i), default=len(lt))", "stop = min((k for k in compact_at if k > i + 1), default=len(lt))"),
    ("waste.py", 'if (u["cache_read_tokens"] or 0) and u["message_id"] not in claimed)', "if True)"),
    ("sources/claude.py", "if complete:  # COALESCE", "if True:  # COALESCE"),
    ("mcp_config.py", '"**/.mcp.json"', '"nothing/.mcp.json"'),
]
TESTS = ["tests.test_deferred_tools", "tests.test_mcp_config"]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.db", "build", "*.egg-info"))
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
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
