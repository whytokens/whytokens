"""Guard the guard: break each first-run rule (empty states, unreadable files, port clash, rescan serialization,
perf budget) in a temp copy of the repo and confirm the first-run tests go red for every one.

    python scripts/falsify_first_run.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the first-run tests red on its own.
MUTANTS = [
    ("static/modules/app.js", """if (/[",\\r\\n]/.test(s)) {""", """if (/[",\\n]/.test(s)) {"""),  # a carriage return splits the row
    ("sources/__init__.py", 'open(filepath, "rb").close()', "pass"),
    ("sources/__init__.py", 'PROGRESS.update(source=source.name, done=0, total=len(files), phase="walk")', "pass"),
    ("api.py", "if not claude and not codex:", "if False:"),
    ("api.py", "elif not claude:", "elif False:"),
    ("api.py", "if not sessions:", "if True:"),
    ("api.py", "Pick another port with --port.", "Pick another port."),
    ("api.py", "            if time.monotonic() >= deadline:", "            if True:"),  # no wait for a port a stopping instance still holds
    # scan banner: a stage never reported, a stale one kept, a count that says nothing, the every-second reload
    ("sources/__init__.py", '    PROGRESS["phase"] = "prepare"', "    pass"),
    ("sources/__init__.py", '    PROGRESS["phase"] = "finish"', "    pass"),
    ("scanrun.py", "errors=[], phase=None)", "errors=[])"),
    ("scanrun.py", 'total=p["total"], phase=p["phase"],', 'total=p["total"], phase=None,'),
    ("static/modules/api.js", "    : sc.total ? 'Reading '", "    : true ? 'Reading '"),
    ("static/modules/api.js", "sc.phase === 'prepare' ? 'Preparing", "false ? 'Preparing"),
    ("static/modules/app.js", "  } else if (wasScanning) loadData();", "  }"),
    ("api.py", "    if scan or os.path.exists(DB_PATH):", "    if scan:"),
    ("api.py", 'running = st is None', 'running = False'),
    ("scanrun.py", "if not _lock.acquire(blocking=False):", "if False:"),
    ("scanrun.py", "        _lock.release()", "        pass"),
    ("scanrun.py", '_state.update(state="error"', '_state.update(state="done"'),
    ("scanrun.py", "projects_dir = root or projects_dir", "pass"),
    ("api.py", "config.discovered_roots(scanrun.projects_dir)", "config.discovered_roots()"),
    ("config.py", '"exists": Path(d).is_dir()}', '"exists": True}'),
    ("scripts/perf_budget.py", "if measured[k] > limit * scale:", "if False:"),
    ("scripts/perf_budget.py", "if measured[k] > limit:", "if False:"),
    # mistyped commands report failure; a flag is never another flag's value
    ("cli.py", 'sys.exit(0 if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h") else 2)', "sys.exit(0)"),
    ("cli.py", 'sys.exit(0 if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h") else 2)', "sys.exit(2)"),
    ("cli.py", '        print("usage: whytokens export --json")\n        sys.exit(2)',
     '        print("usage: whytokens export --json")\n        return'),
    ("cli.py", 'if i + 1 >= len(args) or args[i + 1].startswith("--"):', "if i + 1 >= len(args):"),
    ("cli.py", 'if i + 1 >= len(args) or args[i + 1].startswith("--"):', 'if i + 1 < len(args) and args[i + 1].startswith("--"):'),
    ("cli.py", 'args[i + 1].startswith("--"):', 'args[i + 1].startswith("-"):'),
    # a late response replacing a newer one; CSV cells that start a formula, numbers left alone
    ("static/modules/app.js", "    if (seq !== loadSeq) return;", "    if (false) return;"),
    ("static/modules/app.js", "    if (seq !== loadSeq) return;", "    if (seq === loadSeq) return;"),
    ("static/modules/app.js", """if (/^[=+\\-@\\t\\r]/.test(s) && !isFinite(Number(s))) s = "'" + s;""", ""),
    ("static/modules/app.js", "/.test(s) && !isFinite(Number(s))) s =", "/.test(s)) s ="),
]
TESTS = ["tests.test_first_run"]


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
