"""Guard the guard: break each Waste-tab rule (tiers, evidence, measurement status, one pricing basis,
known-good silence) in a temp copy of the repo and confirm the waste tests go red for every one.

    python scripts/falsify_waste_tab.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the waste tests red on its own.
MUTANTS = [
    ("waste.py", '_MEASURED = {"claude"}', '_MEASURED = {"claude", "codex"}'),
    ("waste.py", '"uncached_input": "observation"', '"uncached_input": "demonstrated saving"'),
    ("waste.py", 'f"{TIERS[cause].capitalize()}: {fix}"', "fix"),
    ("waste.py", '"evidence": top,', '"evidence": [],'),
    ("waste.py", '"session_usd": total', '"session_usd": 0.0'),
    ("waste.py", "floor = max(.01, total * .05)", "floor = 0.0"),
    ("waste.py", '"label": "attributed, lower bound, at API list rate; not savings"', '"label": "savings"'),
    ("waste.py", '"reason": _WHY.get(src, _WHY_DEFAULT)}', '"reason": ""}'),
    ("waste.py", 'sorted({r[0] or "claude" for r in', 'sorted({"claude" for r in'),
    ("waste.py", 'if (row["agent"] or "claude") not in _MEASURED:', 'if False:'),
    ("static/modules/format.js", ".replace(/\"/g, '&quot;')", ""),
    ("api.py", 'r["sessions"] += 1', 'r["sessions"] += 2'),
    ("api.py", '"total_usd": sum(f["usd"] for f in findings)', '"total_usd": 0.0'),
    ("static/index.html", 'data-tab="waste"', 'data-tab="wasted"'),
    ("static/index.html", "Priced at API list rate. Amounts", "Priced at list. Amounts"),
]
TESTS = ["tests.test_waste", "tests.test_waste_tab"]


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
