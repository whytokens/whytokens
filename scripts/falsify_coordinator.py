"""Guard the guard: break each coordinator-spend rule in a temp copy of the repo
and confirm tests/test_coordinator.py goes red for every one.

    python scripts/falsify_coordinator.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
F = "sources/coordinator.py"

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    (F, "        exact = [k for k, d in delta.items() if d == own] if prev is not None else []\n",
        "        exact = []\n"),
    (F, "if d == own] if prev is not None else []", "if d == own]"),           # no-base confirm
    (F, '        t["estimated"] = 0 if len(exact) == 1 else 1', '        t["estimated"] = 0'),
    (F, "        if len(exact) == 1:\n            for k, d", "        if prev is not None:\n            for k, d"),
    (F, "main = exact[0] if len(exact) == 1 else max(", "main = max("),       # largest delta wins
    (F, 'key=lambda d: str(d.get("created_at") or ""))', 'key=lambda d: 0)'),
    (F, 'ts = ts[:23] + "Z" if len(ts) > 23 else ts', 'pass'),
    (F, 'delta[k] = [max(_n(m.get(f)) - _n(base.get(f)), 0) for f in _MU]',
        'delta[k] = [_n(m.get(f)) for f in _MU]'),                       # cumulative, not delta
    # Not mutated: the fallback guess's sort key. With no base, delta IS the
    # cumulative, and with a base an exact match decides first, so a mutant there
    # is equivalent on every reachable input; the guess is flagged estimated anyway.
    (F, '_n(cc.get("ephemeral_5m_input_tokens")), _n(cc.get("ephemeral_1h_input_tokens")),',
        '0, _n(cc.get("ephemeral_1h_input_tokens")),'),
    (F, '                if k != main and any(d):', '                if any(d):'),  # main counted twice
    (F, '    return _s((mu.get(k) or {}).get("canonicalModel")) or k.split("[")[0]', '    return k'),
    (F, '        prev = mu', '        pass'),
    (F, '"project_name": PROJECT,', '"project_name": "",'),
    (F, 'startswith("code:")', 'startswith("")'),
    (F, '        except Exception:  # private cache', '        except ValueError:  # private cache'),
    (F, "            first_timestamp = (SELECT MIN(timestamp)", "            first_timestamp = (SELECT MAX(timestamp)"),
    (F, "    return x if isinstance(x, str) else \"\"", "    return x"),           # malformed speed
    ("sources/__init__.py", "            source.reconcile(conn)", "            pass"),
]


def run(tree):
    # A mutant that drops cycle protection hangs rather than fails: a timeout counts as red.
    try:
        return subprocess.run([sys.executable, "-m", "unittest", "tests.test_coordinator"],
                              cwd=tree, capture_output=True, text=True, timeout=60).returncode
    except subprocess.TimeoutExpired:
        return 124


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
