"""Guard the guard: break the subagent-lane idle check in a temp copy of the repo and confirm
tests/test_waste.py goes red for each break.

    python scripts/falsify_lane_idle.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = "lgap.total_seconds() >= lane[\"ttl\"]"

MUTANTS = [
    ("waste.py", CHECK, "False"),                                   # lane check removed
    ("waste.py", CHECK, "lgap.total_seconds() >= 99999"),           # threshold never reached
    ("waste.py", CHECK, "lgap.total_seconds() >= 0"),               # every turn is idle
    ("waste.py", "if sum(bool(e[\"agent_id\"]) for e in evidence) * 2 > len(evidence) else",
     "if False else"),                                              # subagent fix text dropped
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_waste"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".claude", "build"))
        if run(tree) != 0:
            print("baseline is red; fix the suite first")
            return 1
        for rel, orig, mutant in MUTANTS:
            path = tree / rel
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {rel}: {orig[:60]!r} not found exactly once")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            red = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'ok   ' if red else 'GREEN'}  {orig[:60]!r} -> {mutant[:40]!r}")
            failures += not red
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
