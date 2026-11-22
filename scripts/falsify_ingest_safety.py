"""Guard the guard: break each Claude-ingestion safety rule (final-record precedence,
id-less re-parse dedup, call merging across copies and zero-usage tails, estimate
cleanup only once a replacement exists, malformed records, the redacted backup, the
no-network worktree probe, the pending reconcile, the session start, the decoder
banner) in a temp copy of the repo, one at a time, and confirm the tests go red.

    python scripts/falsify_ingest_safety.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ["tests.test_ingest_safety",
         "tests.test_subagent_estimates.TestAgentSummaryLongPrompt.test_v1_row_is_replaced_on_upgrade_not_kept_beside"]

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # a stop_reason no longer makes the equal-output final record win
    ("scanner.py", "AND ((COALESCE(excluded.stop_reason, '') != ''\n", "AND ((0\n"),
    # a larger cache read on equal output no longer wins
    ("scanner.py", "                                    > turns.input_tokens + turns.cache_read_tokens\n",
     "                                    > 1e18 + turns.input_tokens + turns.cache_read_tokens\n"),
    # a partial copy may downgrade a final row
    ("scanner.py", "                             OR ((COALESCE(excluded.stop_reason, '') = '')\n                                 = (COALESCE(turns.stop_reason, '') = '')",
     "                             OR ((1)"),
    ("scanner.py", "    turns = _drop_stored_id_less(conn, turns)\n", ""),
    ("scanner.py", "    _merge_stored_calls(conn, turns)\n", ""),
    ("scanner.py", '        turns.extend(f for f in full if f.get("message_id") in touched)\n', ""),
    ("scanner.py", "or not (turns or touched or is_subagent_record", "or not (turns or is_subagent_record"),
    ("scanner.py", "        if sid:\n            v1 = AGENT", "        if False:\n            v1 = AGENT"),
    ("scanner.py", "                    if not isinstance(msg, dict):  # a malformed record", "                    if False:  # a malformed record"),
    ("sources/claude.py", "            raise ValueError(errors[0])", "            pass"),
    ("scanner.py", "        _redact_backup(dest)\n", ""),
    ("scanner.py", '    if norm.startswith("//"):\n        return None', '    if False:\n        return None'),
    ("sources/__init__.py", "    if changed or scanner._meta_get(conn, RECONCILE_PENDING):", "    if changed:"),
    ("scanner.py", "OR ? < first_timestamp) THEN ? ELSE first_timestamp END", "OR 0) THEN ? ELSE first_timestamp END"),
    ("scanrun.py", '        _state["result"]["unreadable"] = sum(', '        _state["result"]["unreadable_"] = sum('),
    ("sources/claude.py", 'conn, "final_record_reparse_done_claude", NAME,', 'conn, "merged_tool_calls_reparse_done_claude", NAME,'),
    # a string token count aborts the file again
    ("scanner.py", '                    input_tokens = _tok(usage.get("input_tokens"))', '                    input_tokens = usage.get("input_tokens", 0) or 0'),
    # two copies each carrying one billing detail swap on replay
    ("scanner.py", ">= COALESCE(turns.cache_creation_1h_tokens, 0)", ">= -1"),
    ("scanner.py", "            leftover.unlink(missing_ok=True)  # an unredacted copy", "            pass  # an unredacted copy"),
    ("scanner.py", '    The raw-input opt-in skips only the tool-input rewrite, as on the live DB."""\n',
     '    The raw-input opt-in skips only the tool-input rewrite, as on the live DB."""\n'
     '    if policy.store_raw_inputs():\n        return\n'),
    ("scanner.py", "    turns = [t for t in turns if not (is_advisor_turn(t) and", "    turns = [t for t in turns if not (False and"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
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
