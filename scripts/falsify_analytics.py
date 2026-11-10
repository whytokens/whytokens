"""Guard the guard: break the request-band, waterfall, heatmap and prompt_seq
pass-through rules in a temp copy of the repo, one at a time, and confirm
tests/test_analytics.py goes red for every one.

    python scripts/falsify_analytics.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # Bands: unknown prompt_seq must never make a band.
    ("static/modules/analytics.js", "typeof t.prompt_seq !== 'number' || t.prompt_seq < 1", "t.prompt_seq < 1"),
    ("static/modules/analytics.js", "typeof t.prompt_seq !== 'number' || t.prompt_seq < 1", "typeof t.prompt_seq !== 'number'"),
    ("static/modules/analytics.js", "b.cost += costOf(t);", "b.cost += 0;"),
    ("static/modules/analytics.js", "x.t.prompt_seq === seq", "true"),
    ("static/modules/analytics.js", "map((t, i) => ({ t, i }))", "map((t, i) => ({ t, i: 0 }))"),
    # Waterfall and heatmap.
    ("static/modules/analytics.js", "Math.max(0, total - attributed)", "total - attributed"),
    ("static/modules/analytics.js", "f.session_id === sessionId", "true"),
    ("static/modules/analytics.js", "if (!s || !s.last_date) continue;", ""),
    ("static/modules/analytics.js", "t.setUTCDate(t.getUTCDate() - (t.getUTCDay() + 6) % 7);", "t.setUTCDate(t.getUTCDate() - t.getUTCDay());"),
    ("static/modules/analytics.js", "!(fallbackTotal > 0)", "false"),
    ("static/modules/app.js", "if (wasteWeek && wasteCause && !v.cause) v.groups = [];", "if (false) v.groups = [];"),
    # API passes prompt_seq through.
    ("api.py", "\"prompt_seq\":     r[\"prompt_seq\"],", "\"prompt_seq\":     r[\"prompt_seq\"] or 0,"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_analytics"],
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
