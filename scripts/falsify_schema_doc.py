"""Guard the guard: drop each kind of thing tests/test_schema_doc.py checks, in a temp copy of
the repo, and confirm the test goes red for every one.

    python scripts/falsify_schema_doc.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = "docs/SCHEMA.md"

# (file, original, mutant) - each must turn the test red on its own.
MUTANTS = [
    (DOC, "| `speed` | TEXT |", "| speed | TEXT |"),                              # one column undocumented
    (DOC, "### `schema_meta` — key/value markers", "### schema_meta — key/value markers"),  # one table
    (DOC, "| `recorded_at` | TEXT |", "| `recorded at` | TEXT |"),
    ("scanner.py", "            recorded_at TEXT\n        );", "            recorded_at TEXT,\n            mutant_col  TEXT\n        );"),  # new column, no doc
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_schema_doc"],
                          cwd=tree, capture_output=True, text=True, timeout=120).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".claude", "build"))
        if run(tree) != 0:
            sys.exit("baseline is red; fix that first")
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
