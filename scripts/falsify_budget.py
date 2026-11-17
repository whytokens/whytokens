"""Falsify the budget band and the model what-if: break each piece on a copy of the tree and
check the tests go red. A mutant that stays green is a hole in the tests.

    python scripts/falsify_budget.py        # exit 1 if any mutant is MISSED or STALE
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUD, WHAT, PRICE, API, CLI = "budget.py", "whatif.py", "pricing.py", "api.py", "cli.py"
BJS, WJS, APP, HTML = "static/modules/budget.js", "static/modules/whatif.js", "static/modules/app.js", "static/index.html"
LEAK = '__import__("whatif").reprice("next_tier", db_path=db_path)["delta_usd"]'

MUTANTS = [
    # month_status
    (BUD, "start=(first - timedelta(days=1)).isoformat(),", "start=(today - timedelta(days=7)).isoformat(),"),  # a range filter, not the calendar month
    (BUD, "except (OSError, ValueError, KeyError, TypeError):\n        return None", "except (OSError, ValueError, KeyError, TypeError):\n        return 0.0"),  # unset budget read as zero
    (BUD, "TRAILING_DAYS = 7", "TRAILING_DAYS = 6"),
    (BUD, "if today.day >= TRAILING_DAYS:", "if today.day >= 3:"),
    (BUD, "if budget is not None and remaining else None", "if budget is not None else None"),
    (BUD, "round(projected - budget, 6)", "round(budget - projected, 6)"),
    (BUD, "projected = spent + rate * remaining", "projected = spent"),
    (BUD, "remaining = dim - today.day\n", "remaining = dim - today.day + 1\n"),
    (BUD, 'per_day.get(r["day"], 0.0) + r["cost"]', 'per_day.get(r["day"], 0.0) + r["cost"] / 2'),
    (BUD, "math.isfinite(f) and f > 0", "math.isfinite(f)"),
    (BUD, "path.unlink(missing_ok=True)", "pass"),
    (BUD, "os.replace(tmp, path)", "pass"),
    # budget endpoints and CLI
    (API, "if not self.token or not secrets.compare_digest(sent, self.token):", "if False:"),
    (API, 'if not isinstance(body, dict) or "monthly_usd" not in body:', "if False:"),
    (CLI, "{'over' if over else 'under'}", "{'under'}"),
    # what-if pricing
    (PRICE, 'cache_creation_1h=row["cache_creation_1h"] or 0)', "cache_creation_1h=0)"),  # cache-write rate
    (PRICE, 'row["cache_creation"] or 0, row.get("timestamp")', '0, row.get("timestamp")'),  # cache writes dropped
    (PRICE, 'return price_mult(row.get("speed"), row.get("inference_geo"), to_model, prompt) * calc_cost(', "return 1.0 * calc_cost("),
    (PRICE, 'prompt = (row["input"] or 0) + (row["cache_read"] or 0) + (row["cache_creation"] or 0)', 'prompt = (row["input"] or 0)'),
    (PRICE, "return TIER_ORDER[rank + 1] if rank + 1 < len(TIER_ORDER) else None", "return TIER_ORDER[min(rank + 1, len(TIER_ORDER) - 1)]"),
    # what-if scope and totals
    (WHAT, '(a.agent_type IS NOT NULL OR {side} = 1)', '(a.agent_type IS NULL)'),
    (WHAT, 'clauses.append(f"{day} >= ?")', 'clauses.append(f"{day} > ?")'),
    (WHAT, "if to != NEXT_TIER and not pricing.get_pricing(to):", "if False:"),
    (WHAT, 'priced = [m for m in out if m["to"]]', "priced = out"),
    (WHAT, "if to == NEXT_TIER else to", "if False else to"),
    (API, 'status=400 if "error" in res else 200', "status=200"),
    # no leakage into other totals
    (API, '"total_usd": sum(f["usd"] for f in findings if f.get("in_total", True)),',
     f'"total_usd": sum(f["usd"] for f in findings if f.get("in_total", True)) + {LEAK},'),
    (API, '"cost": round(tot_cost, 6)},', f'"cost": round(tot_cost, 6) + {LEAK}}},'),
    # band wording and links
    (BJS, "`Projected to exceed the budget by ${fmtCostBig(m.gap)}.`", "`Projected to finish ${fmtCostBig(m.gap)}.`"),
    (BJS, "m.budget == null ? 'Not set' : fmtCostBig(m.budget)", "fmtCostBig(m.budget || 0)"),
    (BJS, "(over ? ` <a data-drill", "(true ? ` <a data-drill"),
    (BJS, "(m.budget == null ? ` <a data-drill", "(false ? ` <a data-drill"),
    (BJS, "m.needed_daily > 0 ? `${fmtCostBig(m.needed_daily)}/day` : 'Budget already spent'", "`${fmtCostBig(m.needed_daily)}/day`"),
    (BJS, "month_to_date: 'Projection = spent + month-to-date daily average x days remaining (month-to-date average: fewer than 7 days of data)',",
          "month_to_date: 'Projection = spent + trailing 7-day daily average x days remaining',"),
    (BJS, "export const BASIS = 'estimated at API list price';", "export const BASIS = '';"),
    (APP, "if (gen !== budgetGen || activeTab !== 'overview' || !slot) return;", "if (!slot) return;"),
    (APP, "sessions: drillHref(month, { tab: 'sessions', sort: { col: 'cost', dir: 'desc' } }),",
          "sessions: drillHref(new URLSearchParams(), { tab: 'sessions', sort: { col: 'cost', dir: 'desc' } }),"),
    (APP, "if (!raw || !isFinite(v) || v <= 0) {", "if (false) {"),
    (APP, "function clearBudget() { return saveOrClearBudget(null); }", "function clearBudget() { return saveOrClearBudget(0); }"),
    (APP, "      renderBudgetBand();\n      loadModelWhatif();\n", "      renderBudgetBand();\n"),
    (APP, "${whatifCellHTML(modelWhatif.get(m.model))}", "<td></td>"),
    (APP, "breakdownRowsHTML(sessionId, payload.rows || [], day, whatifLine)", "breakdownRowsHTML(sessionId, payload.rows || [], day)"),
    (APP, "  renderTraceWhatif(d);\n", ""),
    (APP, "window.saveBudget = saveBudget;", ""),
    # what-if wording
    (WJS, "if (!row || !row.to || row.delta_usd == null) return", "if (!row) return"),
    (WJS, "<span class=\"muted\">${esc(WHATIF_WORDING)}</span>`;", "`;"),
    (WJS, "if (!priced.length) return '';", ""),
    (HTML, 'id="budget-band"', 'id="budget-bnd"'),
    (HTML, "If on next tier down", "If cheaper"),
    ("whatif.py", "(a.agent_type IS NOT NULL OR {side} = 1)", "(a.agent_type IS NOT NULL)"),
    ("whatif.py", "(a.agent_type IS NULL AND {side} = 0)", "(a.agent_type IS NULL)"),
    ("budget.py", "    except OverflowError:", "    except KeyError:"),
    ("budget.py", "    path.parent.mkdir(parents=True, exist_ok=True)\n", ""),
    ("budget.py", 'fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")  # one temp file per writer',
     'tmp = str(path) + ".tmp"; fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)'),
    ("whatif.py", 'if tz == "local"\n               else "substr', 'if False\n               else "substr'),
    (APP, "catch (e) { if (gen !== whatifGen) return; modelWhatif", "catch (e) { modelWhatif"),
    (APP, "    sessionLanes.clear();  // a rescan", "    //  // a rescan"),
    (APP, "if (sessionLanes.get(sessionId) === p)", "if (true)"),
    (APP, "{ to: 'next_tier', start, end, tz: displayTZ }", "{ to: 'next_tier', start, end }"),
]
TESTS = ["tests.test_budget", "tests.test_whatif", "tests.test_budget_ui", "tests.test_drilldown", "tests.test_server_hardening"]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    caught = missed = stale = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.db", "build", "*.egg-info", ".claude", ".local-delegate"))
        if run(tree) != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig[:60]!r}")
                stale += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            red = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if red else 'MISSED'} {fname}: {orig.strip()[:60]}")
            caught += red
            missed += not red
    print(f"caught {caught}, MISSED {missed}, STALE {stale}")
    sys.exit(1 if missed or stale else 0)


if __name__ == "__main__":
    main()
