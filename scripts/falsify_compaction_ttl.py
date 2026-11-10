"""Guard the guard: break the user-record compaction carry, its backfill, the partial-expiry
branch, both TTL boundaries and the TTL-keyed idle_gap in a temp copy of the repo, one at a time,
and confirm tests/test_compaction_ttl.py goes red for every one.

    python scripts/falsify_compaction_ttl.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("scanner.py", "pending_compact[session_id] = None", "pass"),
    ("sources/claude.py", "WHERE message_id = ? AND is_compact_summary = 0", "WHERE message_id = ? AND 0"),
    ("waste.py", "if read and gap is not None and gap.total_seconds() >= 300", "if False and gap is not None and gap.total_seconds() >= 300"),
    ("waste.py", "gap.total_seconds() >= prev[\"ttl\"]:", "gap.total_seconds() > prev[\"ttl\"]:"),
    ("static/modules/pricing.js", "t.gap_s >= (t.cache_creation_1h", "t.gap_s > (t.cache_creation_1h"),
    ("scanner.py", "if t.get(\"message_id\") in compact:", "if False:"),
    ("sources/claude.py", "if complete:", "if True:"),
    ("sources/claude.py", "if not any(usage.get(k) or 0", "if False and not any(usage.get(k) or 0"),
    ("waste.py", "gap.total_seconds() >= previous_main[\"ttl\"]", "gap.total_seconds() >= 1800"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_compaction_ttl"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".claude"))
        if run(tree) != 0:
            print("baseline is red; fix the suite first")
            return 1
        for rel, orig, mutant in MUTANTS:
            path = tree / rel
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {rel}: {orig.strip()[:60]!r} not found exactly once")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            red = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if red else 'MISSED'}  {rel}: {orig.strip()[:60]!r}")
            failures += not red
    print(f"{len(MUTANTS) - failures}/{len(MUTANTS)} mutants caught")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
