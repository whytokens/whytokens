"""Guard the guard: break each Changes-screen rule in a temp copy of the repo and
confirm tests/test_changes.py goes red for every one.

    python scripts/falsify_changes.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each pattern is one line and must turn the suite red on its own.
MUTANTS = [
    ("api.py", "            except (sqlite3.Error, OSError) as e:  # e.g. the settings file is locked", "            except ZeroDivisionError as e:  # e.g. the settings file is locked"),
    ("fixes.py", "MIN_SESSIONS = 10", "MIN_SESSIONS = 5"),
    ("fixes.py", "    elif a < MIN_SESSIONS or", "    elif False and"),
    ("fixes.py", "    if b < MIN_SESSIONS:", "    if False:"),
    ("fixes.py", "    if when > (today or date.today()).isoformat():", "    if False:"),
    ("fixes.py", "    if not note:", "    if False:"),
    ("fixes.py", "AND id != ?\"", "AND id = ?\""),
    ("fixes.py", "rate = _recent_rate(db_path, fix[\"project\"], today)", "rate = 0"),
    ("api.py", "    if not note.strip():", "    if False:"),
    ("api.py", "    if not isinstance(payload, dict):\n        raise ValueError(\"body must be a JSON object\")\n    when, note", "    when, note"),
    ("fixes.py", "    conn.execute(\"BEGIN IMMEDIATE\")\n    try:\n        _update_fix", "    try:\n        _update_fix"),
    ("fixes.py", "    if when is not None:\n        _canon_date(when)", "    if False:\n        _canon_date(when)"),
    ("api.py", "isinstance(payload.get(\"id\"), bool) or ", ""),
    ("static/modules/changes.js", "const interim = !!c.interim;", "const interim = false;"),
    ("static/modules/changes.js", "if (c.state === 'reduced') h +=", "if (true) h +="),
    ("static/modules/changes.js", "if (!confirm(`Delete", "if (false && !confirm(`Delete"),
    ("static/modules/changes.js", "if (c.state !== 'waiting') return '';", "if (false) return '';"),
    ("static/modules/changes.js", "if (c.eta_days == null) return", "if (false) return"),
    ("api.py", "    everything = _gathered(fixes, db_path) if listed else None  # one pass for every change, not one each", "    everything = None"),
    ("api.py", "    everything = _gathered(fixes, db_path) if listed else None  # one pass for every fix, not one each", "    everything = None"),
    ("fixes.py", "    if fix[\"cause\"] in waste.UNCOUNTED:", "    if False:"),
    ("fixes.py", "\"large_context\": \"Context carried past 250K\", ", ""),
    ("api.py", "        if not out[\"projects\"]:", "        if False:"),
    ("static/modules/changes.js", "if (!d.projects.includes(pre)) f.project.add(new Option(pre, pre));", ""),
    ("static/modules/changes.js", "c.saving_usd == null ?", "false ?"),
    ("api.py", "except (ValueError, TypeError, OverflowError, RecursionError) as e:  # ValueError includes", "except (ValueError, RecursionError) as e:  # ValueError includes"),
    ("api.py", "except (sqlite3.Error, OSError) as e:  # e.g. the DB is locked by a scan", "except ZeroDivisionError as e:  # e.g. the DB is locked by a scan"),
    # a build without model help (public): no adapter routes, no page flag, CLI says so, help omits it
    ("api.py", 'elif path == "/api/llm" and HAS_LLM:', 'elif path == "/api/llm":'),
    ("api.py", 'HAS_LLM and path in ("/api/llm/preview", "/api/llm/run")', 'path in ("/api/llm/preview", "/api/llm/run")'),
    ("api.py", "            if HAS_LLM:  # the page loads llm.js", "            if True:  # the page loads llm.js"),
    ("api.py", "            if HAS_LLM:  # the page loads llm.js", "            if False:  # the page loads llm.js"),
    ("cli.py", '        print("whytokens llm: model help is not part of this build")\n        sys.exit(2)', "        return"),
    ("cli.py", "    return USAGE if has_llm else re.sub(", "    return USAGE if True else re.sub("),
    ("cli.py", '    return USAGE if has_llm else re.sub(r"  whytokens llm .*?\\n(?=  whytokens )"',
               '    return USAGE if has_llm else re.sub(r"  whytokens llm .*?\\n"'),
    ("static/modules/app.js", """(document.querySelector('meta[name="whytokens-llm"]') ? import('./llm.js') : Promise.reject())""",
                              "import('./llm.js')"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_changes"],
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
