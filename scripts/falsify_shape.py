"""Guard the guard: break each work-shape rule in a temp copy of the repo and confirm
tests/test_shape.py goes red for every one.

    python scripts/falsify_shape.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each original must occur exactly once.
MUTANTS = [
    ("shape.py", "    if cats[top] / n >= DOMINANT_SHARE:", "    if True:"),                      # threshold ignored
    ("shape.py", "    if n < MIN_TOOL_CALLS:", "    if False:"),                              # no chat floor
    ("shape.py", "                m = median(group)", "                m = sum(group) / len(group)"),  # mean instead of median
    ("shape.py", "(\"project\", by_project[r[\"project\"]])", "(\"shape\", by_project[r[\"project\"]])"),  # fallback silent
    ("shape.py", "            if len(group) >= MIN_PEERS:", "            if len(group) >= 1:"),   # peer minimum ignored
    ("shape.py", "    return x is not None and x >= OUTLIER_X", "    return x is not None and x > OUTLIER_X"),  # 2.0x not flagged
    ("shape.py", "r[\"prompts\"] > 0 and ", ""),                                                # unknown prompts counted
    ("shape.py", "cpp=r[\"usd\"] / r[\"prompts\"]", "cpp=r[\"usd\"]"),                         # per session, not per prompt
    ("shape.py", "        if f.get(\"in_total\", True):", "        if True:"),                     # observation summed into overhead
    ("shape.py", "    sig = dict(conn.execute(_SIG))", "    open(__file__).close()\n    sig = dict(conn.execute(_SIG))"),  # backfill reads files
    ("shape.py", "s.turn_count + COALESCE(t.n, 0)", "s.turn_count"),                          # backfill not re-derived
    ("budget.py", "os.O_CREAT | os.O_EXCL | os.O_WRONLY", "os.O_CREAT | os.O_WRONLY"),          # lock never blocks
    ("api.py", 'if "tag" not in payload:', "if False:"),
    ("shape.py", "st != sig.get(sid)", "st is None"),                      # new turns not re-derived
    ("api.py", "\"outliers\": sum(1 for p in", "\"outlier_usd\": 1.0, \"outliers\": sum(1 for p in"),  # outlier given a cost
    ("api.py", "    if not known:", "    if False:"),                                          # tag on unknown project
    ("fixes.py", "    r[\"mix\"] = _mix(data, fix)", "    r[\"mix\"] = _mix(data, fix); r[\"status\"] = \"increased\" if r[\"mix\"][\"warn\"] else r[\"status\"]"),  # shape changes verdict
    ("fixes.py", "        if shift[top] >= shape.MIX_SHIFT:", "        if shift[top] > shape.MIX_SHIFT:"),  # warning edge
    ("fixes.py", "    if min(n.values()) >= MIN_SESSIONS:", "    if True:"),                       # warn while waiting
    ("fixes.py", "            if min(sides[\"before\"][\"sessions\"], sides[\"after\"][\"sessions\"]) >= MIN_SESSIONS:", "            if True:"),  # same-shape gate
    ("budget.py", "            d.pop(\"monthly_usd\", None)", "            d.clear()"),                  # clearing the budget drops tags
    ("budget.py", "    if len(tag) > TAG_MAX or any(ord(c) < 32 for c in tag):", "    if False:"),  # tag unchecked
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_shape"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".local-delegate"))
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
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}", flush=True)
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
