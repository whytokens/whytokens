"""Falsify the Overview-cards test in tests/test_review_regressions.py: put the dollar cards back
on a different basis than the charts, on a copy of the tree, and check the test goes red.

    python scripts/falsify_overview_cards.py   # exit 1 if any mutant is MISSED or STALE
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = "static/modules/app.js"
CARDS = "const costTotals = aggregateCostBreakdown(filteredDaily);"

MUTANTS = [
    (APP, CARDS, "const costTotals = aggregateCostBreakdown(rangeFilteredSessions);"),  # whole sessions by last day
    (APP, CARDS, "const costTotals = aggregateCostBreakdown(rawData.daily_by_model);"),  # range and model filter dropped
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_review_regressions"],
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
                print(f"STALE  {fname}: pattern not found once: {orig[:60]!r}")
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
