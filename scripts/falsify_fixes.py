"""Guard the guard: break each `verify` / `fix record` rule in a temp copy of
the repo and confirm tests/test_fixes.py goes red for every one.

    python scripts/falsify_fixes.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("fixes.py", "waste.report(db_path, min_share=0, min_usd=0)", "waste.report(db_path, min_share=0)"),
    ("fixes.py", "if u > 0]", "]"),
    ("fixes.py", "when = date.fromisoformat(when).isoformat()", "date.fromisoformat(when)"),
    ("fixes.py", '("reduced" if delta > 0 else "increased")', '"reduced"'),
    ("fixes.py", 'delta = before["pct"] - after["pct"]', 'delta = after["pct"] - before["pct"]'),
    ("fixes.py", "MIN_SESSIONS = 10", "MIN_SESSIONS = 0"),
    ("fixes.py", 'if min(before["sessions"], after["sessions"]) < MIN_SESSIONS', 'if max(before["sessions"], after["sessions"]) < MIN_SESSIONS'),
    ("fixes.py", 'out["saving_usd"] = delta * after["usd"]', 'out["saving_usd"] = before["cause_usd"]'),
    ("fixes.py", 'if f["cause"] == fix["cause"]:', "if True:"),
    ("fixes.py", "material = abs(delta) >= MIN_DELTA_PP and abs(delta) >= MIN_DELTA_REL * before[\"pct\"]", "material = True"),
    ("fixes.py", "needs at least", "needs about"),
    ("fixes.py", "if d < fix[\"fix_date\"]]", "if d < \"0000\"]"),
    ("scanner.py", '"tool_results", "fixes")', '"tool_results")'),
    ("api.py", "        if not self._guard(post=True):", "        if not self._guard():"),
    ("api.py", "or not isinstance(note, str):", "or False:"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_fixes"],
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
