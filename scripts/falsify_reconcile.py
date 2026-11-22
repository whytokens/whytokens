"""Falsify the reconcile view: break each cause row, the cut, storage and the CLI on a copy of
the tree and check tests/test_reconcile.py goes red. A mutant that stays green is a hole.

    python scripts/falsify_reconcile.py        # exit 1 if any mutant is MISSED or STALE
"""

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKERS = min(4, os.cpu_count() or 1)
REC, BUD, CLI = "reconcile.py", "budget.py", "cli.py"
TESTS = ["tests.test_reconcile"]

MUTANTS = [
    # the cut
    (REC, 'day = "substr(timestamp, 1, 10)" if tz == "utc" else LOCAL_DAY', 'day = "substr(timestamp, 1, 10)"'),
    (REC, 'upto = " AND wt_ts(timestamp) < ?" if as_of else ""', 'upto = ""'),
    (REC, 'utc = f"wt_ts(timestamp) >= ? AND wt_ts(timestamp) < ?{upto}"',
     'utc = f"wt_ts(timestamp) >= ? AND substr(wt_ts(timestamp), 1, 10) <= substr(?, 1, 10){upto}"'),
    # the exact-instant cutoff: julianday() rounds below a millisecond
    (REC, 'upto = " AND wt_ts(timestamp) < ?" if as_of else ""',
     'upto = " AND julianday(timestamp) < julianday(?)" if as_of else ""'),
    # each cause row
    (REC, "usd=shown[0] - utc_all[0]", "usd=0.0"),
    (REC, 'codex = _cost(conn, f"{utc} AND NOT ({NOT_CODEX})", p)', 'codex = (0.0, 0)'),
    (REC, 'summaries = _cost(conn, f"{utc} AND {NOT_CODEX} AND {SUMMARY}", p)',
     'summaries = _cost(conn, f"{utc} AND {NOT_CODEX} AND estimated = 1", p)'),  # advisor estimates swept in
    (REC, "premium = long_full[0] * (1 - 1 / pricing.LONG_PROMPT_MULT)", "premium = long_full[0]"),
    (REC, ' AND NOT {SUMMARY}"\n               f" AND NOT {ADVISOR_EST}', '"\n               f" AND NOT {ADVISOR_EST}'),  # rows a and b overlap
    (REC, "AND COALESCE(agent, 'claude') = 'claude'\")", "\")"),  # coordinator rows judged per request
    (REC, "remainder = None if gap is None else gap - sum(r[\"usd\"] for r in rows)",
     "remainder = None if gap is None else gap - sum(r[\"usd\"] for r in rows[:2])"),
    # coverage
    (REC, "before = (0 if earliest and earliest[:10] < first.isoformat() else", "before = (0 if earliest else"),
    # storage
    (REC, "if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:",
     "if not isinstance(v, (int, float)) or v < 0:"),
    (REC, "d = budget._read(path, strict=True)", "d = budget._read(path)"),
    (BUD, "        if strict:\n            raise", "        if False:\n            raise"),
    (REC, 'raise ValueError(f"no turns for session', 'print(f"no turns for session'),
    # API and panel
    ("api.py", 'tz = _qs1(q, "tz") or "local"\n        if tz not in ("local", "utc"):',
     'tz = _qs1(q, "tz") or "local"\n        if False:'),
    ("api.py", "as_of = body[\"as_of\"] if body.get(\"as_of\") is not None else reconcile.default_as_of(period)",
     "as_of = body.get(\"as_of\")"),
    ("static/modules/app.js", "  refreshReconcile = initReconcile(() => displayTZ);\n", ""),
    ("static/modules/app.js", "  displayTZ = mode;\n  refreshReconcile?.();", "  displayTZ = mode;"),
    # review round: as_of type, NULL message ids, future periods, fractional seconds
    (REC, '"as_of": None if as_of is None else _instant(as_of)', '"as_of": as_of and _instant(as_of)'),
    (REC, "SUMMARY = f\"COALESCE(message_id, '') LIKE", "SUMMARY = f\"message_id LIKE"),
    (REC, "    _not_future(bounds(period)[0])\n", "    bounds(period)\n"),
    (REC, 'return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")', 'return t.strftime("%Y-%m-%dT%H:%M:%SZ")'),
    (REC, 'upto = " AND wt_ts(timestamp) < ?" if as_of else ""',
     'upto = " AND wt_ts(timestamp) < ?" if as_of and as_of < end else ""'),
    # CLI
    (CLI, 'print(f"  gap (shown - billed):  {money(r[\'gap\'])}")', "pass"),
]
def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
                          cwd=tree, capture_output=True, text=True).returncode


def check(tree, mutant):
    fname, orig, mut = mutant
    path = tree / fname
    src = path.read_text(encoding="utf-8")
    if src.count(orig) != 1:
        return "STALE", f"STALE  {fname}: pattern not found once: {orig[:60]!r}"
    path.write_text(src.replace(orig, mut), encoding="utf-8")
    red = run(tree) != 0
    path.write_text(src, encoding="utf-8")
    return ("caught" if red else "MISSED"), f"{'caught' if red else 'MISSED'} {fname}: {orig.strip()[:60]}"


def main():
    # each mutant is ~8s of tests, so ~67 of them in series is minutes: one tree copy per worker
    workers = min(WORKERS, len(MUTANTS))
    counts = {"caught": 0, "MISSED": 0, "STALE": 0}
    with tempfile.TemporaryDirectory() as tmp:
        trees = []
        for i in range(workers):
            tree = Path(tmp) / f"repo{i}"
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.db", "build", "*.egg-info", ".claude", ".local-delegate"))
            trees.append(tree)
        if run(trees[0]) != 0:
            sys.exit("baseline suite is red; fix that first")
        jobs = [MUTANTS[i::workers] for i in range(workers)]
        with ThreadPoolExecutor(workers) as pool:
            for results in pool.map(lambda a: [check(a[0], m) for m in a[1]], zip(trees, jobs)):
                for kind, line in results:
                    print(line)
                    counts[kind] += 1
    print(f"caught {counts['caught']}, MISSED {counts['MISSED']}, STALE {counts['STALE']}")
    sys.exit(1 if counts["MISSED"] or counts["STALE"] else 0)


if __name__ == "__main__":
    main()
