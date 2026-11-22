"""Break each cost-layer rule one at a time and check tests/test_cost_layers.py goes red.

Run: python scripts/falsify_cost_layers.py   (exit 0 = every mutant caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PJS = "static/modules/pricing.js"

MUTANTS = [
    # layer classification
    ("pricing.py", " OR {m} LIKE '{adv}%:est'", ""),
    ("pricing.py", "THEN 'estimated'", "THEN 'recorded'"),
    ("pricing.py", "THEN 'advisor'", "THEN 'recorded'"),
    # drop each layer from the sum
    ("cli.py", 'out["total"] = sum(out[k] for k in pricing.LAYERS)', 'out["total"] = sum(out[k] for k in pricing.LAYERS[1:])'),
    ("cli.py", 'out["total"] = sum(out[k] for k in pricing.LAYERS)', 'out["total"] = sum(out[k] for k in pricing.LAYERS[::2])'),
    ("cli.py", 'out["total"] = sum(out[k] for k in pricing.LAYERS)', 'out["total"] = sum(out[k] for k in pricing.LAYERS[:2])'),
    ("api.py", '            L["estimated"] += fee\n', ""),
    ("api.py", 'for L in (layers, d["layers"]):', "for L in (layers,):"),
    ("api.py", "        cost += fee\n", ""),
    (PJS, "    L.estimated += c.web_search;\n", ""),
    (PJS, "L[r.layer || 'recorded'] += c.total - c.web_search;", "L[r.layer || 'recorded'] += c.total;"),
    (PJS, "  c.total += c.web_search;\n", ""),
    # charge errored calls
    ("pricing.py", "AND COALESCE(is_error, 0) = 0\"", "\""),
    # charge a pending call (no result yet)
    ("pricing.py", "result_bytes IS NOT NULL AND ", ""),
    # the 5x premium must not scale the fee
    ("reconcile.py", 'long_full = _token_cost(conn, f"session_id = ? AND {LONG_PROMPT}"',
     'long_full = _cost(conn, f"session_id = ? AND {LONG_PROMPT}"'),
    # trace turns carry the fee
    ("api.py", '"web_searches":   r["web_searches"],', '"web_searches":   0,'),
    # double the fee, server and client
    ("pricing.py", "WEB_SEARCH_FEE_USD = 0.01", "WEB_SEARCH_FEE_USD = 0.02"),
    (PJS, "WEB_SEARCH_FEE = (_fees && _fees.web_search_usd) || 0;", "WEB_SEARCH_FEE = 2 * ((_fees && _fees.web_search_usd) || 0);"),
    # the fee reaches every surface
    ("reconcile.py", 'dollars = cost_layers(conn, where, params)["total"]', 'dollars = cost_layers(conn, where, params)["recorded"]'),
    ("api.py", '"_fees": {"web_search_usd": pricing.WEB_SEARCH_FEE_USD}', '"_fees": {}'),
    ("cli.py", '    print(f"                    {layer_line(layers)}")\n', ""),
    # a syntax error anywhere in app.js (the eval harnesses only load slices of it)
    ("static/modules/app.js", "function layerLine(L) {", "function layerLine(L) { '"),
]
TESTS = "tests.test_cost_layers"


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", TESTS], cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__", "*.db", "docs", "build"))
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
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]} -> {mutant.strip()[:30]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
