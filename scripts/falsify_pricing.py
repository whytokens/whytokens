"""Guard the guard: break, one at a time in a temp copy of the repo, the price resolver's
match order and its "how was this id priced" answer, the list of fallback-priced models
(DB helper and API), the fast-mode speed switch as an expected cache rebuild, and the UTC
"Last N days" step, the version-number boundary in both resolvers, and the JS
keyword fallbacks. Each mutant must turn its tests red.

    python scripts/falsify_pricing.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRICING, MISS, RANGE, HTTP = "tests.test_pricing", "tests.test_cache_miss", "tests.test_range_bounds", "tests.test_dashboard"

# (file, original, mutant, test module) - each must turn that module red on its own.
MUTANTS = [
    # a guessed prefix match reported as the id's own row
    ("pricing.py", 'return key, "exact" if _DATED.fullmatch(model[len(key):]) else "prefix"', 'return key, "exact"', PRICING),
    # a dated snapshot of a listed model reported as a guess
    ("pricing.py", 'return key, "exact" if _DATED.fullmatch(model[len(key):]) else "prefix"', 'return key, "prefix"', PRICING),
    # prefix scan no longer first-wins in table order (a 5.2 id falls to the 5.0 row)
    ("pricing.py", "    for key in PRICING:\n        if model.startswith(key) and", "    for key in reversed(list(PRICING)):\n        if model.startswith(key) and", PRICING),
    ("pricing.py", '    (("fable-5-2", "mythos-5-2"), "claude-fable-5-2"),\n', "", PRICING),
    ("pricing.py", 'return ("gpt-5.4-mini" if key == "gpt-5.4" and "mini" in m else key), "keyword"', 'return key, "keyword"', PRICING),
    ("pricing.py", 'return ("gpt-5.4-mini" if key == "gpt-5.4" and "mini" in m else key), "keyword"', 'return ("gpt-5.4-mini" if key == "gpt-5.4" and "mini" in m else key), "exact"', PRICING),
    # the fallback list includes models that have their own row
    ("pricing.py", 'if how in ("prefix", "keyword")]', "if how]", PRICING),
    ("pricing.py", "    if not Path(db_path).exists():\n        return []", "    if False:\n        return []", PRICING),
    ("pricing.py", "    except sqlite3.OperationalError:\n        return []", "    except ZeroDivisionError:\n        return []", PRICING),
    ("api.py", 'self._send_json({"models": pricing.fallbacks_in_db(DB_PATH)})', "self._send_json({})", HTTP),
    # the endpoint answers without reading the served DB
    ("api.py", 'self._send_json({"models": pricing.fallbacks_in_db(DB_PATH)})', 'self._send_json({"models": []})', HTTP),
    ("api.py", 'self._send_json({"models": pricing.fallbacks_in_db(DB_PATH)})', 'self._send_json({"models": pricing.fallbacks_in_db(config.DB_PATH)})', HTTP),
    # speed switch: not recognised / claimed whenever fast is involved / previous speed not carried / label reversed
    ("waste.py", "    if was_fast != is_fast:", "    if False:", MISS),
    ("waste.py", "    if was_fast != is_fast:", "    if was_fast or is_fast:", MISS),
    ("waste.py", '"compact": compact, "speed": turn["speed"],', '"compact": compact, "speed": "",', MISS),
    ("waste.py", "f\"speed switch ({'fast' if was_fast else 'standard'} to {'fast' if is_fast else 'standard'})\"",
     "f\"speed switch ({'fast' if is_fast else 'standard'} to {'fast' if was_fast else 'standard'})\"", MISS),
    ("waste.py", "{speed} AS speed, {mult} AS mult", "NULL AS speed, {mult} AS mult", MISS),
    # UTC display: stepping back in local days, or one day too many
    ("static/modules/app.js", "if (displayTZ === 'utc') d.setUTCDate(", "if (false) d.setUTCDate(", RANGE),
    ("static/modules/app.js", "d.setUTCDate(d.getUTCDate() - (days - 1));", "d.setUTCDate(d.getUTCDate() - days);", RANGE),
    # version boundary: a dated 5.0 id ("claude-fable-5-20260610") read as 5.2, prefix or keyword
    ("pricing.py", "if model.startswith(key) and not _splits_number(key, model, 0):", "if model.startswith(key):", PRICING),
    ("pricing.py", "if any(_contains(m, w) for w in words):", "if any(w in m for w in words):", PRICING),
    ("pricing.py", "    return _ascii_digit(word[-1:]) and _ascii_digit(s[end:end + 1])", "    return False", PRICING),
    ("pricing.py", "    return c.isascii() and c.isdigit()", "    return c.isdigit()", PRICING),  # Unicode digit: Python and JS disagree
    ("static/modules/pricing.js", "if (model.startsWith(key) && !splitsNumber(key, model, 0)) return key;", "if (model.startsWith(key)) return key;", PRICING),
    ("static/modules/pricing.js", "if (!splitsNumber(word, s, at)) return true;", "return true;", PRICING),
    # the JS 5.2 / 5.1 fallbacks dead (Codex 11-10: source-string tests stayed green)
    ("static/modules/pricing.js", "if (has(m, 'fable-5-2') || has(m, 'mythos-5-2'))", "if (false && (has(m, 'fable-5-2') || has(m, 'mythos-5-2')))", PRICING),
    ("static/modules/pricing.js", "if (has(m, 'fable-5-1') || has(m, 'mythos-5-1'))", "if (false)", PRICING),
    ("static/modules/pricing.js", "if (has(m, 'fable') || has(m, 'mythos'))", "if (has(m, 'fable'))", PRICING),
]


def run(tree, module):
    return subprocess.run([sys.executable, "-m", "unittest", module],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db", ".claude", "build"))
        for module in sorted({m[3] for m in MUTANTS}):
            if run(tree, module) != 0:
                print(f"baseline is red in {module}; fix the suite first")
                return 1
        for rel, orig, mutant, module in MUTANTS:
            path = tree / rel
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {rel}: {orig.strip()[:60]!r} not found exactly once")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8", newline="\n")
            red = run(tree, module) != 0
            path.write_text(src, encoding="utf-8", newline="\n")
            print(f"{'caught  ' if red else 'SURVIVED'} {rel}: {mutant.strip()[:70]!r}")
            failures += not red
    print(f"{len(MUTANTS) - failures}/{len(MUTANTS)} mutants caught")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
