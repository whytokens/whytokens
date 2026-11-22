"""Guard the guard: break each fast-mode / US-geo / Haiku 5.5 long-prompt pricing rule in a temp copy of
the repo and confirm tests/test_price_modifiers.py (or the module a mutant names) goes red for every one.

    python scripts/falsify_price_modifiers.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    ("pricing.py", "THEN 2.0 ELSE 1.0 END\" if", "THEN 1.0 ELSE 1.0 END\" if"),
    ("pricing.py", "THEN 1.1 ELSE 1.0 END\"", "THEN 1.0 ELSE 1.0 END\""),
    ("pricing.py", "(2.0 if speed", "(1.0 if speed"),
    ("pricing.py", "(1.1 if inference_geo", "(1.0 if inference_geo"),
    ("pricing.py", "if \"speed\" in cols else \"1.0\"", "if \"speed\" in cols else \"speed\""),
    ("pricing.py", "    return x if any(x.values()) else None", "    return None"),
    ("scanner.py", '"speed": usage.get("speed", "") or "",', '"speed": "",'),
    ("scanner.py", '                            speed="",\n', ""),
    ("scanner.py", "            speed                    = excluded.speed,\n",
     "            speed                    = turns.speed,\n"),
    ("sources/claude.py", '.get("speed") == "fast":', '.get("speed") == "nope":'),
    ("api.py", "* pricing.price_mult(t.get(\"speed\")", "* pricing.price_mult(\"\""),
    ("api.py", "                pricing.surcharge_cost(r[\"model\"], r, r[\"rate_day\"]))", "                0)"),
    ("cli.py", "            surcharge_cost(r[\"model\"], r, timestamp))", "            0)"),
    ("waste.py", "row[\"timestamp\"], h1) * row[\"mult\"]", "row[\"timestamp\"], h1)"),
    ("static/modules/pricing.js", "  if (!x) return c;", "  return c;"),
    # Haiku 5.5 long prompts (5x over 100K)
    ("pricing.py", "THEN {LONG_PROMPT_MULT} ELSE 1.0 END\"", "THEN 1.0 ELSE 1.0 END\""),
    ("pricing.py", "f\" > {LONG_PROMPT_TOKENS}", "f\" >= {LONG_PROMPT_TOKENS}"),
    ("pricing.py", "{t}input_tokens + {t}cache_read_tokens + ", "{t}input_tokens + "),
    ("pricing.py", "f\"{t}input_tokens + {t}cache_read", "f\"{t}cache_read"),
    ("pricing.py", " + {t}cache_creation_tokens\"", "\""),
    ("api.py", "+ t.get(\"cache_creation_tokens\", 0)) for t in turns)", "+ 0) for t in turns)"),
    ("pricing.py", "if set(LONG_PROMPT_COLS) <= set(cols) else", "if False else"),
    # Dated tiers resolve per turn day in CLI aggregates (Sonnet 5.5 cache-read cut)
    ("cli.py", "    costs = dated_costs(conn)\n", "    costs = {r[\"model\"]: row_cost(r) for r in by_model}\n"),
    ("pricing.py", "(LONG_PROMPT_MULT if long_prompt else 1.0)", "1.0"),
    ("pricing.py", "prompt_tokens > LONG_PROMPT_TOKENS", "prompt_tokens >= LONG_PROMPT_TOKENS"),
    ("api.py", "t.get(\"inference_geo\"), t.get(\"model\"),", "t.get(\"inference_geo\"), \"\","),
    ("api.py", "t.get(\"input_tokens\", 0) + t.get(\"cache_read_tokens\", 0)", "t.get(\"input_tokens\", 0)"),
    ("static/modules/pricing.js", "const c = rowCostBreakdown(m,", "const c = calcCostBreakdown(m.model, m.input, m.output, m.cache_read, m.cache_creation, day, m.cache_creation_1h) || rowCostBreakdown(m,"),
    # AgentSummary estimates on Haiku 5.5: grouped per row under the 100K line, old summed rows re-read
    ("scanner.py", "if pricing.LONG_PROMPT_MODEL in (last.get(\"model\") or \"\").lower() else math.inf)",
     "if False else math.inf)", "tests.test_subagent_estimates"),
    ("scanner.py", "+ chunks[-1][1] + c > cap:", "> cap:", "tests.test_subagent_estimates"),
    ("scanner.py", "message_id=base + (\"#%d\" % i if i else \"\")))", "message_id=base))",
     "tests.test_subagent_estimates"),
    ("sources/claude.py", "conn, marker, NAME, why=\"to price AgentSummary", "conn, \"x\", NAME, why=\"to price AgentSummary",
     "tests.test_subagent_estimates"),
    ("scanner.py", "[(m,) for m in stale])", "[])", "tests.test_subagent_estimates"),
    ("scanner.py", "(m[len(base):len(base) + 1] == \"#\" and m[len(base) + 1:].isdigit())", "True",
     "tests.test_subagent_estimates"),
    ("scanner.py", "                    OR excluded.timestamp != turns.timestamp))", "                    ))",
     "tests.test_subagent_estimates"),
    ("scanner.py", "timestamp=ts if i < len(chunks) - 1 else", "timestamp=", "tests.test_subagent_estimates"),
    ("scanner.py", "keep = summaries.setdefault(t[\"summary_of\"], set())",
     "keep = summaries.setdefault(\":\".join(t[\"message_id\"].split(\":\")[:3]), set())",
     "tests.test_subagent_estimates"),
    ("scanner.py", "and (m == base or (", "and (False or (",
     "tests.test_subagent_estimates"),
    ("scanner.py", "                t[\"summary_of\"] = base\n", "                pass\n",
     "tests.test_subagent_estimates"),
]
TESTS = "tests.test_price_modifiers"


def run(tree, tests=TESTS):
    return subprocess.run([sys.executable, "-m", "unittest", tests],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db"))
        if run(tree) != 0 or run(tree, "tests.test_subagent_estimates") != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant, *tests in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig!r}")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            caught = run(tree, *tests) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
