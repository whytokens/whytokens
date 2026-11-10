"""Guard the guard: break each Waste-tab rule (tiers, evidence, measurement status, one pricing basis,
known-good silence) in a temp copy of the repo and confirm the waste tests go red for every one.

    python scripts/falsify_waste_tab.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the waste tests red on its own.
MUTANTS = [
    ("waste.py", '_MEASURED = {"claude"}', '_MEASURED = {"claude", "codex"}'),
    ("waste.py", '"uncached_input": "observation"', '"uncached_input": "demonstrated saving"'),
    ("waste.py", 'f"{tier.capitalize()}: {fix}"', "fix"),
    ("waste.py", '"evidence": top,', '"evidence": [],'),
    ("waste.py", '"session_usd": total', '"session_usd": 0.0'),
    ("waste.py", "floor = max(min_usd, total * min_share)", "floor = 0.0"),
    ("waste.py", '"label": "attributed, lower bound, at API list rate; not savings"', '"label": "savings"'),
    ("waste.py", '"reason": _WHY.get(src, _WHY_DEFAULT)}', '"reason": ""}'),
    ("waste.py", 'sorted({r[0] or "claude" for r in', 'sorted({"claude" for r in'),
    ("waste.py", 'if (row["agent"] or "claude") not in _MEASURED and', 'if False and'),
    # Codex resident_output (inferred)
    ("waste.py", 'if turn["agent"] in _INFERRED:', 'if turns[0]["agent"] in _INFERRED:'),
    ("waste.py", 'if state["tool"] else 0', "if True else 0"),
    ("waste.py", "min(ctx - state[\"ctx\"], turn[\"input_tokens\"] or 0)", "ctx"),
    ("waste.py", '"tier": tier,', '"tier": TIERS[cause],'),
    ("waste.py", '{"status": "inferred", "reason"', '{"status": "not measurable", "reason"'),
    ("static/modules/format.js", ".replace(/\"/g, '&quot;')", ""),
    ("api.py", 'r["sessions"] += 1', 'r["sessions"] += 2'),
    ("api.py", '"total_usd": sum(f["usd"] for f in findings)', '"total_usd": 0.0'),
    ("static/index.html", 'data-tab="waste"', 'data-tab="wasted"'),
    ("static/index.html", "Priced at API list rate. Amounts", "Priced at list. Amounts"),
    # subagent lanes and synthetic rows
    ("waste.py", 'if creation and not lane["preamble"]:', "if creation:"),
    ("waste.py", 'lane = lanes.setdefault(turn["agent_id"],', "lane = lanes.setdefault(None,"),
    ("waste.py", 'if turn["is_sidechain"] and not turn["agent_id"]:', "if False:"),
    ("waste.py", 'if turn["is_sidechain"] and not turn["agent_id"]:', 'if turn["is_sidechain"] and turn["agent_id"] is None:'),
    ("waste.py", 'elif creation and not compact and prev and prev["has_tool"]:', 'elif creation and prev and prev["has_tool"]:'),
    ("waste.py", 'elif creation and not compact and prev and prev["repeated"]:', "elif False:"),
    ("waste.py", '_tool_info(turn, lane["seen"])', "_tool_info(turn, seen_paths)"),
    ("waste.py", "mid.startswith((ADVISOR_ID_PREFIX, AGENT_SUMMARY_ID_PREFIX))", "mid.startswith((ADVISOR_ID_PREFIX,))"),
    ("waste.py", "mid.startswith((ADVISOR_ID_PREFIX, AGENT_SUMMARY_ID_PREFIX))", "mid.startswith((AGENT_SUMMARY_ID_PREFIX,))"),
    ("waste.py", '\n            or (not mid and row["tool_name"] == "advisor" and not row["tool_calls"]))', ")"),
    ("waste.py", '**{c: c if c in cols else "NULL" for c in ("message_id", "agent_id", "speed")}',
     '**{c: c for c in ("message_id", "agent_id", "speed")}'),
    # Waste view: scope, cause bars, habits grouped by project x cause
    ("static/modules/app.js", "  const inScope = findings.filter(f => byId.has(f.session_id));", "  const inScope = findings;"),
    ("static/modules/app.js", "    affectedUsd: [...spend.values()].reduce((sum, v) => sum + v, 0),",
     "    affectedUsd: inScope.reduce((sum, f) => sum + f.session_usd, 0),"),
    ("static/modules/app.js", "    if (cause && f.cause !== cause) continue;\n", ""),
    ("static/modules/app.js", "    const c = causes.get(f.cause) || { cause: f.cause, tier: f.tier, usd: 0, sessions: 0, observed: 0, inferred: 0 };\n",
     "    if (cause && f.cause !== cause) continue;\n    const c = causes.get(f.cause) || { cause: f.cause, tier: f.tier, usd: 0, sessions: 0, observed: 0, inferred: 0 };\n"),
    ("static/modules/app.js", "  if (!causes.has(cause)) cause = null;\n", ""),
    ("static/modules/app.js", "const g = groups.get(key) ||", "const g = groups.get(f.cause) ||"),
    ("static/modules/app.js", "title: s.topic || ''", "title: s.topic"),
    ("static/modules/app.js", "b.sessions.length - a.sessions.length", "0"),
    ("static/modules/app.js", "  for (const g of groups.values()) g.sessions.sort(byUsd);\n", ""),
    ("static/modules/app.js", "g.usd += f.usd; g.spend += f.session_usd;", "g.usd += f.usd; g.spend = f.session_usd;"),
    ("static/modules/app.js", "document.querySelectorAll('.range-btn[data-range]').forEach(btn =>\n    btn.classList",
     "document.querySelectorAll('.range-btn').forEach(btn =>\n    btn.classList"),
    # overhead copy: avoidability tags, headline, concrete fix strings
    ("static/modules/app.js", "    if (f.attribution === 'inferred') g.fix = f.fix;", "    if (false) g.fix = f.fix;"),
    ("static/modules/app.js", "detail: f.detail, fix: f.fix, evidence", "detail: f.detail, evidence"),
    ("static/modules/app.js", "  idle_gap: ['mostly avoidable',", "  idle_gapx: ['mostly avoidable',"),
    ("static/modules/app.js", "${avoidTag(c.cause)}", ""),
    ("static/modules/app.js", "${fmtCostBig(v.total)} in overhead</div>", "${fmtCostBig(v.total)} attributed to waste</div>"),
    ("static/modules/app.js", "'<div>Not all of it can be cut:", "'<div>All of it can be cut:"),
    ("waste.py", "Read with offset/limit, Grep before Read", "Trim tool output"),
    ("waste.py", '"and cut long command output to its tail or a summary." if inferred else',
     '"and cut long command output to its tail or a summary." if False else'),
]
TESTS = ["tests.test_waste", "tests.test_waste_tab", "tests.test_waste_view", "tests.test_waste_codex"]


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
