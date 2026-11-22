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
    ("fixes.py", "waste.report(db_path, min_share=0, min_usd=0,", "waste.report(db_path, min_share=0,"),
    ("fixes.py", " OR message_id LIKE '{waste.AGENT_SUMMARY_ID_PREFIX}%'", ""),             # summary copies counted
    ("fixes.py", 'if fix["cause"] not in lost.get(r[0], ())]', "]"),                        # lost evidence reads as 0
    ("fixes.py", "{before['unmeasured'] + after['unmeasured']} more could not", "{0} more could not"),  # reason hides the count
    ("fixes.py", "but {before['unmeasured'] + after['unmeasured']} sessions", "but {0} sessions"),        # same, decided side
    ("fixes.py", 'if watch.execute("PRAGMA data_version").fetchone()[0] == before:', "if True:"),  # two DB states
    ("waste.py", 'lost |= {"resident_output", "repeated_reads"}', "pass"),
    ("waste.py", "if any(cid not in errs for cid in cids):", "if False:"),                           # lost flags read as 0
    ("waste.py", "if any(cid not in errs for cid in cids):", "if cids and not any(cid in errs for cid in cids):"),  # one flag is enough
    ("waste.py", "if unknown[0] or any(n is None for t in claude for n in _aligned(t, sizes, by_id)):", "if unknown[0]:"),
    ("waste.py", "if any(cid not in loaded_ids for cid in searches):", "if False:"),             # unknown loads read as 0
    ("fixes.py", 'if material and (before["unmeasured"] or after["unmeasured"]):', "if False:"),  # selective loss decides
    ("fixes.py", "if u > 0]", "]"),
    ("fixes.py", "when = date.fromisoformat(when).isoformat()", "date.fromisoformat(when)"),
    ("fixes.py", '("reduced" if delta > 0 else "increased")', '"reduced"'),
    ("fixes.py", 'delta = before["pct"] - after["pct"]', 'delta = after["pct"] - before["pct"]'),
    ("fixes.py", "MIN_SESSIONS = 10", "MIN_SESSIONS = 0"),
    ("fixes.py", 'if min(before["sessions"], after["sessions"]) < MIN_SESSIONS', 'if max(before["sessions"], after["sessions"]) < MIN_SESSIONS'),
    ("fixes.py", 'out["saving_usd"] = delta * after["usd"]', 'out["saving_usd"] = before["cause_usd"]'),
    ("fixes.py", 'cause_usd.get(fix["cause"], {})', '{k: v for d in cause_usd.values() for k, v in d.items()}'),
    ("fixes.py", "material = round(abs(delta), 9) >= MIN_DELTA_PP and abs(delta) >= MIN_DELTA_REL * before[\"pct\"]", "material = True"),
    ("fixes.py", "round(abs(delta), 9) >= MIN_DELTA_PP", "abs(delta) >= MIN_DELTA_PP"),  # exactly one point
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
