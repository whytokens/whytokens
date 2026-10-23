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
    ("sources/__init__.py", 'open(filepath, "rb").close()', "pass"),
    ("sources/__init__.py", "PROGRESS.update(source=source.name, done=0, total=len(files))", "pass"),
    ("api.py", "if not claude and not codex:", "if False:"),
    ("api.py", "elif not claude:", "elif False:"),
    ("api.py", "if not sessions:", "if True:"),
    ("api.py", "Pick another port with --port.", "Pick another port."),
    ("api.py", 'running = st is None', 'running = False'),
    ("scanrun.py", "if not _lock.acquire(blocking=False):", "if False:"),
    ("scanrun.py", "        _lock.release()", "        pass"),
    ("scanrun.py", '_state.update(state="error"', '_state.update(state="done"'),
    ("scanrun.py", "projects_dir = root or projects_dir", "pass"),
    ("api.py", "config.discovered_roots(scanrun.projects_dir)", "config.discovered_roots()"),
    ("config.py", '"exists": Path(d).is_dir()}', '"exists": True}'),
    ("scripts/perf_budget.py", "if measured[k] > limit * scale:", "if False:"),
    ("scripts/perf_budget.py", "if measured[k] > limit:", "if False:"),
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
