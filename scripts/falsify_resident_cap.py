"""Guard the guard: break the resident_output result-size cap in a temp copy of the repo,
one rule at a time, and confirm tests/test_waste.py goes red for every one.

Rules: the claim is min(next write, estimated tool-result tokens) on the main chain AND in
subagent lanes; per-call ranking figures are capped with it; an unknown result size claims
nothing, is never read as 0 bytes, and is counted in the finding's detail.

    python scripts/falsify_resident_cap.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL_WRITE = '_claim(claims, "resident_output", turn, creation, [("tool", creation)], "tool")'

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    # The cap itself, shared by both claim sites.
    ("waste.py", "    claim = min(creation, est)\n", "    claim = creation if est else 0\n"),
    # Each site must route through the cap (a fix to only one would leave the other charging the write).
    ("waste.py", "                    _resident(claims, turn, creation, prev, unknown)\n",
     f"                    {FULL_WRITE}\n"),
    ("waste.py", "                    _resident(claims, turn, creation, previous_main, unknown)\n",
     f"                    {FULL_WRITE}\n"),
    # Per-call ranking uses the capped figures, not the raw estimates.
    ("waste.py", "    per_call = [(name, n * scale) for name, n in known]\n", "    per_call = known\n"),
    # Unknown size: never the whole write, never silently dropped, never read as 0 bytes.
    ("waste.py", "    claim = min(creation, est)\n", "    claim = creation if missing else min(creation, est)\n"),
    ("waste.py", "    unknown[0] += missing\n", ""),
    ("waste.py", "for name, n in sizes.get(row[\"message_id\"], ()) if n is not None]",
     "for name, n in [(a, b or 0) for a, b in sizes.get(row[\"message_id\"], ())]]"),

    # next_step_action: each rule, the floor, the pick, the ranking, the null and the unknown-size guard.
    ("waste.py", '    if tool == "Read":\n        return "read_ranges", tool\n', '    if tool == "Read":\n        return "bash_tail", tool\n'),
    ("waste.py", '    if tool == "Bash":\n        return "bash_tail", tool\n', '    if tool == "Bash":\n        return "read_ranges", tool\n'),
    ("waste.py", 'if tool in ("Grep", "Glob"):', 'if tool in ("Grep",):'),
    ("waste.py", '    return ("mcp_text", m.group(1)) if m else None', '    return None'),
    ("waste.py", 'c["result_tokens"] >= OVERSIZED_TOKENS:', 'c["result_tokens"] >= 0:'),
    ("waste.py", 'key=lambda kv: sum(c["result_tokens"] for c in kv[1]))', 'key=lambda kv: len(kv[1]))'),
    ("waste.py", 'key=lambda c: c["result_tokens"], reverse=True)[:5]', 'key=lambda c: c["result_tokens"])[:5]'),
    ("waste.py", '    if not groups:\n        return None\n', '    if not groups:\n        return {"id": "read_ranges", "title": "", "rule": "", "calls": [], "est_usd": 0, "est_basis": ""}\n'),
    ("waste.py", 'if names.count(name) == 1:', 'if names:'),
    ("waste.py", 'if _synthetic(turn) or last[turn["agent_id"]] == i:', 'if _synthetic(turn):'),
    ("waste.py", 'for name, n in sizes.get(turn["message_id"], ()) if n is not None]\n        names', 'for name, n in sizes.get(turn["message_id"], ())]\n        names'),
    # idle_gap / subagent_preamble actions
    ("waste.py", 'rid = "subagent_waits" if side else "compact_before_break"', 'rid = "compact_before_break"'),
    ("waste.py", 'sum(bool(e["agent_id"]) for e in evidence) * 2 > len(evidence)\n    rid', 'sum(bool(e["agent_id"]) for e in evidence) * 2 < len(evidence)\n    rid'),
    ("waste.py", '"est_usd": sum(c["usd"] - c["warm_usd"] for c in calls)', '"est_usd": sum(c["usd"] for c in calls)'),
    ("waste.py", 'turns_by_lane.get(e["agent_id"], SMALL_JOB_TURNS) < SMALL_JOB_TURNS]', 'turns_by_lane.get(e["agent_id"], 0) < 99]'),
    ("waste.py", 'if e["agent_id"] and turns_by_lane', 'if turns_by_lane'),
    ("waste.py", '    if not small:\n        return None\n', '    if not small:\n        small = [dict(message_id="m", at="t", agent_type=None, turns=0, usd=0)]\n'),
    ("waste.py", '(types or {}).get(e["agent_id"]) if e["agent_id"] else None', 'None'),
    ("waste.py", 'len(lanes)} subagents', 'len(evidence)} subagents'),
    ("waste.py", '"est_usd": sum(c["usd"] for c in small)', '"est_usd": 0'),
    # repeated_reads: the key, the forgetting, the cap
    ("waste.py", 'key = (path, inp.get("offset"), inp.get("limit"))', 'key = (path, None, None)'),
    ("waste.py", 'if call.get("name") in _WRITERS:', 'if False:'),
    ("waste.py", '                seen_paths.clear()  # compaction dropped what was read; this turn', '                pass  # compaction dropped what was read; this turn'),
    ("waste.py", 'est = min(creation, sum(known))', 'est = creation'),
    ("waste.py", '    if est < 1:\n        return False', '    if est < 0:\n        return False'),
    ("waste.py", '    unknown[0] += len(sized) - len(known)', '    pass'),
    ("waste.py", 'positional = len(res) == len(calls) and all(', 'positional = all('),
    ("waste.py", 'c.get("name") == r[0] for c, r in zip(calls, res))', 'True for c, r in zip(calls, res))'),
    ("waste.py", 'n = by_id.get(cid) if cid else (res[i][1] if positional else None)', 'n = res[i][1] if i < len(res) else None'),
    ("waste.py", '    if rest >= 1:', '    if False:'),
    ("waste.py", 'h1 = h1 * min(creation, total) / total if total else h1', 'h1 = h1'),
    ("waste.py", '            has_tool, repeated, rep_idx = _tool_info(turn, seen_paths)', '            has_tool, repeated, rep_idx = _tool_info(turn, seen_paths) if not compact else (False, [], [])'),
    ("policy.py", 'if isinstance(v, int) and not isinstance(v, bool):\n            out[key] = v', 'if v is not None:\n            out[key] = v'),
]



def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", "tests.test_waste", "tests.test_waste_action", "tests.test_repeated_reads"],
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
