"""Guard the guard: break each Changes-screen rule in a temp copy of the repo and
confirm tests/test_changes.py goes red for every one.

    python scripts/falsify_changes.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each pattern is one line and must turn the suite red on its own.
MUTANTS = [
    ("fixes.py", "MIN_SESSIONS = 10", "MIN_SESSIONS = 5"),
    ("fixes.py", "    elif a < MIN_SESSIONS:", "    elif False:"),
    ("fixes.py", "    if b < MIN_SESSIONS:", "    if False:"),
    ("fixes.py", "    if when > (today or date.today()).isoformat():", "    if False:"),
    ("fixes.py", "    if not note:", "    if False:"),
    ("fixes.py", "AND id != ?\"", "AND id = ?\""),
    ("fixes.py", "rate = _recent_rate(db_path, fix[\"project\"], today)", "rate = 0"),
    ("api.py", "    if not note.strip():", "    if False:"),
    ("api.py", "    if not isinstance(payload, dict):\n        raise ValueError(\"body must be a JSON object\")\n    when, note", "    when, note"),
    ("fixes.py", "    conn.execute(\"BEGIN IMMEDIATE\")\n    try:\n        _update_fix", "    try:\n        _update_fix"),
    ("fixes.py", "    if when is not None:\n        _canon_date(when)", "    if False:\n        _canon_date(when)"),
    ("api.py", "isinstance(payload.get(\"id\"), bool) or ", ""),
    ("static/modules/changes.js", "const interim = !!c.interim;", "const interim = false;"),
    ("static/modules/changes.js", "if (c.state === 'reduced') h +=", "if (true) h +="),
    ("static/modules/changes.js", "if (!confirm(`Delete", "if (false && !confirm(`Delete"),
    ("static/modules/changes.js", "if (c.state !== 'waiting') return '';", "if (false) return '';"),
    ("static/modules/changes.js", "if (c.eta_days == null) return", "if (false) return"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_changes"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db"))
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
