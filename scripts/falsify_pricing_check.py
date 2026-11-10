"""Guard the guard: break each `pricing check` rule in a temp copy of
the repo and confirm tests/test_pricing_check.py goes red for every one.

    python scripts/falsify_pricing_check.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("pricing_check.py", "            if not _valid(name, data):", "            if False:"),
    ("pricing_check.py", "        if _valid(name, cached[\"data\"]):", "        if True:"),
    ("pricing_check.py", "math.isfinite(x) and x >= 0", "x >= 0"),
    ("pricing_check.py", "|-\d{4}-\d{2}-\d{2})$", ")$"),
    ("pricing_check.py", "if c in best and best[c] >= rank:", "if c in best:"),
    ("pricing_check.py", "rank = (key.lower() == c, ", "rank = (False, "),
    ("pricing_check.py", "return abs(a - b) > TOLERANCE * max(abs(a), abs(b), 1e-9)", "return False"),
    ("pricing_check.py", r'm = re.sub(r"(?:[@-]\d{8}|-\d{4}-\d{2}-\d{2})$", "", m)', "pass"),
    ("pricing_check.py", 'm = model_id.lower().split("/")[-1]', 'm = model_id.lower()'),
    ("pricing_check.py", "    if not offline:", "    if True:"),
    ("pricing_check.py", "return x * 1e6 if", "return x * 1e3 if"),
    ("pricing_check.py", "active = pricing._resolve_tiers(entry)", "active = entry[0] if isinstance(entry, list) else entry"),
    ("pricing_check.py", "    except FileNotFoundError:", "    except ZeroDivisionError:"),
    ("pricing_check.py", '"agrees" if not _differs(check[f], rates[f]) else "DISAGREES"', '"agrees"'),
    ("pricing_check.py", "if direct and m not in shipped", "if False and m not in shipped"),
    ("pricing_check.py", "if model not in src:", "if False:"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_pricing_check"],
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
