"""Guard the guard: break the migration verify and the /ext containment (and route
precedence) in a temp copy of the repo and confirm tests/test_extensions.py goes red
for every one.

    python scripts/falsify_extensions.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("config.py", "if _counts(tmp) != _counts(legacy):", "if False:"),
    ("config.py", "        os.replace(tmp, new)\n    except BaseException:\n        for f in (tmp, new):",
     "        os.replace(tmp, new)\n    except BaseException:\n        for f in (new,):"),
    ("extensions.py", "            cand.relative_to(root)\n        except ValueError:\n            continue",
     "            pass\n        except ValueError:\n            continue"),
    ("api.py", "        elif path == \"/api/extensions\":", "        elif path == \"/api/extensions\" or True:"),
    ("api.py", "        elif path.startswith(\"/ext/\"):", "        elif False:"),
    ("sources/__init__.py", "        if ext.get(\"on_scan\"):", "        if False:"),
]


def run(tree):
    try:
        return subprocess.run([sys.executable, "-m", "unittest", "tests.test_extensions"],
                              cwd=tree, capture_output=True, text=True, timeout=120).returncode
    except subprocess.TimeoutExpired:
        sys.exit("a step timed out (120s); not counting it as caught")


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.db", "build", "*.egg-info", ".local-delegate"))
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
