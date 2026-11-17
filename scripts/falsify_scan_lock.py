"""Guard the guard: break the cross-process scan lock, the CLI's handling of it, the tolerant column
migration, the pre-migration backup's race and leftover handling, the CLI's migrate-before-read and the
exclusive dashboard port in a temp copy of the repo, one at a time, and confirm
tests/test_scan_lock.py goes red for every one.

    python scripts/falsify_scan_lock.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("sources/__init__.py", "    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)", "    pass"),  # first scan into a missing data directory
    # the lock is never taken
    ("sources/__init__.py", "    with _scan_lock(db_path):\n        return _scan_all(", "    if True:\n        return _scan_all("),
    ("sources/__init__.py", "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)", "pass") if sys.platform == "win32" else
    ("sources/__init__.py", "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)", "pass"),
    # a stale lock: the file's existence, not an OS lock, blocks the scan
    ("sources/__init__.py", '    f = open(f"{db_path}.scanlock", "a+b")\n',
     '    if os.path.exists(f"{db_path}.scanlock"):\n        raise ScanBusy("stale")\n    f = open(f"{db_path}.scanlock", "a+b")\n'),
    # the lock is kept after the scan
    ("sources/__init__.py", "    finally:\n        f.close()\n", "    finally:\n        globals().setdefault('_leak', []).append(f)\n"),
    ("cli.py", "    except sources.ScanBusy as e:\n        raise SystemExit", "    except ZeroDivisionError as e:\n        raise SystemExit"),
    ("scanner.py", 'if "duplicate column name" not in str(e):\n                        raise', "raise"),
    ("scanner.py", 'if "duplicate column name" not in str(e):', "if False:"),
    ("scanner.py", "    if bak.exists() and bak.stat().st_size:\n        return bak", "    if bak.exists():\n        return bak"),
    ("scanner.py", "    tmp.unlink(missing_ok=True)  # a leftover", "    pass  # a leftover"),
    ("scanner.py", "        if not (bak.exists() and bak.stat().st_size):\n            raise", "        pass"),
    ("scanner.py", "            os.link(tmp, bak)\n", "            os.replace(tmp, bak)\n"),  # a later copy replaces the first backup
    ("scanner.py", "            if not (bak.exists() and bak.stat().st_size):\n                os.replace(tmp, bak)", "            pass"),  # no hard links: no backup at all
    ("scanner.py", "    except OSError:  # Windows: another process", "    except ZeroDivisionError:  # Windows: another process"),
    ("cli.py", 'if command not in ("dashboard", "scan", "doctor", "live", "pricing") and DB_PATH.exists():', "if False:"),
    ("shape.py", '        conn.execute("BEGIN IMMEDIATE")', "        pass"),
    ("api.py", "    with _waste_lock:\n        return _waste_payload(waste, db_path)", "    return _waste_payload(waste, db_path)"),
    ("api.py", "        if not (_gather_cache and _gather_cache[0] == key):", "        if True:"),
    ("api.py", "            _gather_cache = (key, fixes._gather_all(db_path))\n        return _gather_cache[1]", "            return fixes._gather_all(db_path)\n        return _gather_cache[1]"),
    ("api.py", "    request_queue_size = 128", "    request_queue_size = 5"),
    ("api.py", 'allow_reuse_address = os.name != "nt"', "allow_reuse_address = True") if sys.platform == "win32" else None,
]
MUTANTS = [m for m in MUTANTS if m]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_scan_lock"],
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
