"""Guard the guard: break the backup-before-migration guard, the NULL-not-zero
rule and the prompt_seq/size parsing in a temp copy of the repo, one at a time, and
confirm tests/test_tool_results.py goes red for every one.

    python scripts/falsify_tool_results.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # Backup before any schema change.
    ("scanner.py", "def init_db(conn):\n    backup_before_migration(conn)\n", "def init_db(conn):\n"),
    ("scanner.py", "    if \"turns\" not in tables:\n        return False",
     "    if True:\n        return False"),
    ("scanner.py", "    return any(not set(cols) <= _table_cols(conn, table)", "    return False and any(not set(cols) <= _table_cols(conn, table)"),
    ("scanner.py", "    if bak.exists():\n        return bak\n", ""),
    ("scanner.py", "        conn.backup(dest)\n", "        pass\n"),
    # NULL, never 0.
    ("scanner.py", "    \"prompt_seq\": \"INTEGER\",", "    \"prompt_seq\": \"INTEGER DEFAULT 0\","),
    ("scanner.py", "            prompt_seq              INTEGER\n", "            prompt_seq              INTEGER DEFAULT 0\n"),
    ("scanner.py", "message_id=message_id or None, tool_name=b.get(\"name\"))",
     "message_id=message_id or None, tool_name=b.get(\"name\"), result_bytes=0)"),
    ("scanner.py", "                WHEN EXISTS (\n", "                WHEN 0 AND EXISTS (\n"),
    ("scanner.py", "WHEN EXISTS (SELECT 1 FROM tool_results r", "WHEN 0 AND EXISTS (SELECT 1 FROM tool_results r"),
    ("scanner.py", "    \"\"\", [(r[\"call_id\"],) for r in rows])\n", "    \"\"\", [])\n"),
    ("scanner.py", " and set(CORE_INDEXES) <= indexes", ""),
    ("scanner.py", "            t[\"prompt_seq\"] = f.get(\"prompt_seq\")\n", "            pass\n"),
    ("scanner.py", "b.get(\"type\") in (\"image\", \"document\")", "b.get(\"type\") in ()"),
    ("sources/claude.py", "_, turns, _ = scanner.parse_jsonl_file(filepath, calls_out=calls)",
     "_, turns, _ = scanner._parse_file(filepath, 0, None, None, calls)"),
    ("scanner.py", "!= 'claude' THEN NULL", "!= 'nope' THEN NULL"),
    ("scanner.py", "message_id   = COALESCE(excluded.message_id, tool_results.message_id)",
     "message_id   = excluded.message_id"),
    # prompt_seq.
    ("scanner.py", "\"prompt_seq\": None if sidechain else prompt_seq,", "\"prompt_seq\": prompt_seq,"),
    ("scanner.py", "            t[\"prompt_seq\"] = seq.get(t.get(\"message_id\"))\n", "            pass\n"),
    ("scanner.py", "    while text.startswith(_REMINDER_OPEN):", "    while False:"),
    ("scanner.py", "    \"<side-sessions-event>\", ", "    "),
    ("scanner.py", "origin.get(\"kind\") not in (None, \"human\")", "origin.get(\"kind\") == \"peer\""),
    ("scanner.py", "record.get(\"isMeta\") or record.get(\"isCompactSummary\")", "record.get(\"isCompactSummary\")"),
    # Sizes and the backfill.
    ("scanner.py", "            n = _utf8_len(c)\n", "            n = len(c)\n"),
    ("sources/claude.py", "                scanner.upsert_tool_results(conn, calls.values())\n", ""),
    ("sources/__init__.py", "            scanner.upsert_tool_results(conn, tool_results)\n", ""),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_tool_results"],
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
