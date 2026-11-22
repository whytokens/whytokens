"""Falsify tests/test_carried_rereads.py: break each rule carried re-reads depend on, one at a time,
in a copy of the tree, and check the suite goes red. Exit 1 on any mutant missed."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MUTANTS = [
    # carry start: the next call writes the item, reads start one call after that
    ("waste.py", "        if i + 2 >= end[i]:\n", "        if i >= end[i]:\n"),  # the call just before a compaction carries nothing
    ("waste.py", "        items.append((i + 2, end[i], OWN_OUTPUT,", "        items.append((i + 1, end[i], OWN_OUTPUT,"),
    ("waste.py", "            items.append((i + 2, end[i], label,", "            items.append((i + 1, end[i], label,"),
    # compaction cutoff: exclusive, and it resets at every compaction
    ("waste.py", "        if turns[i][\"is_compact_summary\"]:\n            nxt = i\n",
     "        if turns[i][\"is_compact_summary\"]:\n            nxt = i + 1\n"),
    ("waste.py", "        if turns[i][\"is_compact_summary\"]:\n            nxt = i\n", "        pass\n"),
    # call-to-result join: the path comes from the result's own call id
    ("waste.py", "                path = paths.get(cid)\n", "                path = next(iter(paths.values()), None)\n"),
    ("waste.py", "if path else READ_PATH_UNKNOWN)", "if path else \"Read:other\")"),
    ("waste.py", "for cid, tool, nbytes in results.get(t[\"message_id\"], ()):",
     "for cid, tool, nbytes in results.get(turns[i - 1][\"message_id\"] if i else None, ()):"),
    # lane split: session + subagent, never the session alone
    ("waste.py", "lanes.setdefault((r[\"session_id\"], r[\"agent_id\"]), [])", "lanes.setdefault((r[\"session_id\"], \"\"), [])"),
    # cap at the reading call's cache read
    ("waste.py", "rate * min(1.0, cr / run) if run > 0 else 0.0", "rate if run > 0 else 0.0"),
    # coverage: sessions with unknown result sizes are excluded, never read as 0
    ("waste.py", "if b is not None}\n        results = {}", "}\n        results = {}"),
    # window: whole lanes are loaded, only in-window reads are charged
    ("waste.py", "sql += \" AND session_id IN (SELECT DISTINCT session_id FROM turns WHERE timestamp >= ?)\"",
     "sql += \" AND timestamp >= ?\""),
    ("waste.py", "                if t.get(\"in_window\", True) else 0.0)", "                if True else 0.0)"),
    # a subagent call with no agent id never merges into the main lane
    ("waste.py", "if _synthetic(r) or (r[\"is_sidechain\"] and not r[\"agent_id\"]):", "if _synthetic(r):"),
]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_carried_rereads"],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".claude", "build"))
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
