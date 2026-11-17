"""Falsify the rework scorer (rework.py) and the blind labelling tool: break each rule on a copy of
the tree and check tests/test_rework.py goes red. A mutant that stays green is a hole in the tests.

    python scripts/falsify_rework.py        # exit 1 if any mutant is MISSED or STALE
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RW, LT = "rework.py", "scripts/label_rework.py"

MUTANTS = [
    # D1
    (RW, "sum(1 for v in res.values() if v) / prompts", "len(res) / prompts"),  # successes counted as errors
    (RW, "all(res.get(cid) is not None for cid in known)", "True"),
    (RW, "quote(Path(db_path).resolve().as_posix(), safe='/:')", "Path(db_path).resolve().as_posix()"),  # '#' or '?' in the path read as URI syntax
    (RW, "all(res.get(cid) is not None for cid in known)", "all(v is not None for v in res.values())"),
    # prompts: from the prompt records, not the replies
    (RW, 'prompts = len(marks["prompt_times"]) if marks else max_seq[sid]', "prompts = max_seq[sid]"),
    (RW, '                    times.append(_ts(r.get("timestamp")))', '                    times.append(_ts(r.get("timestamp")) and None)'),
    # D2
    (RW, "if seen.setdefault(path, seq) < seq:", "if seen.setdefault(path, seq) <= seq:"),  # same-prompt re-edit
    (RW, "if seen.setdefault(path, seq) < seq:", "if False:"),
    # D3
    (RW, '_text(msg.get("content")).startswith("[Request interrupted")', 'False'),
    (RW, '"interrupt_rate": marks["interrupts"] / prompts if marks else None,', '"interrupt_rate": marks["interrupts"] / prompts if marks else 0,'),
    # D4
    (RW, "(b - a).total_seconds() > GAP_MIN * 60 for a, b", "(b - a).total_seconds() >= 0 for a, b"),
    (RW, '"followups": prompts / requests - 1 if requests else None,', '"followups": prompts / requests if requests else None,'),
    (RW, '"followups": prompts / requests - 1 if requests else None,', '"followups": prompts / requests - 1 if requests else 0,'),
    # D5
    (RW, 'if out[a]["prompts"] > SHORT_PROMPTS or end is None:', 'if end is None:'),
    (RW, '(nxt - end).total_seconds() <= GAP_MIN * 60', 'True'),
    (RW, "if b != a and t >= end), None)", "if b != a), None)"),
    # transcript lookup
    (RW, "found = [p for p in found if p.name == name and p.is_file()]", "found = [p for p in found if p.is_file()]"),
    (RW, "found = [p for p in found if p.name == name and p.is_file()]", "found = [p for p in found if p.name == name][:1]"),
    (RW, 'best = max(reads, key=lambda m: len(m["prompt_times"]))', "best = reads[0]"),
    (RW, 'return {**best, "limit_hit": any(m["limit_hit"] for m in reads)}', "return best"),
    (RW, "no_id.add(sid)  # a call", "pass  # a call"),
    (RW, 'if marks and len(marks["prompt_times"]) < max_seq[sid]:', "if False:"),
    (RW, "marks, limit = None, (True if limit else None)", "marks, limit = None, limit"),
    (RW, "unordered.add(sid)  # a main-thread edit", "pass  # a main-thread edit"),
    # limit stops
    (RW, 'r.get("error") == "rate_limit" or "usage limit"', 'False and "usage limit"'),
    (RW, 'limit = marks["limit_hit"] if marks else None', 'limit = marks["limit_hit"] if marks else False'),
    (RW, 'if m is None or m["limit_hit"] is not False or', 'if m is None or m["limit_hit"] is True or'),
    (RW, 'if m is None or m["limit_hit"] is not False or', 'if m is None or'),
    # scorer
    (RW, "wins = sum((r > f) + 0.5 * (r == f) for r in rework for f in fine)", "wins = sum((r > f) for r in rework for f in fine)"),
    (RW, "wins = sum((r > f) + 0.5 * (r == f) for r in rework for f in fine)", "wins = sum((f > r) + 0.5 * (r == f) for r in rework for f in fine)"),
    (RW, "if not rework or not fine:\n        return None", "if not rework or not fine:\n        return 0.5"),
    # blind labelling tool
    (LT, 'if sid not in labelled and m["limit_hit"] is False and m["prompts"] >= 2:', 'if m["prompts"] >= 2:'),
    (LT, '            f"first prompt: {first or \'(not found)\'}', '            f"{m}\\nfirst prompt: {first or \'(not found)\'}'),
    (LT, "                save(labels, labels_path)\n", ""),
]
TESTS = ["tests.test_rework"]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS], cwd=tree, capture_output=True, text=True).returncode


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
            path.write_text(src.replace(orig, mutant), encoding="utf-8", newline="\n")
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError:  # a broken mutant goes red on import and proves nothing
                path.write_text(src, encoding="utf-8", newline="\n")
                print(f"STALE  {fname}: mutant does not compile: {mutant[:60]!r}")
                stale += 1
                continue
            red = run(tree) != 0
            path.write_text(src, encoding="utf-8", newline="\n")
            print(f"{'caught' if red else 'MISSED'} {fname}: {orig.strip()[:60]}")
            caught += red
            missed += not red
    print(f"caught {caught}, MISSED {missed}, STALE {stale}")
    sys.exit(1 if missed or stale else 0)


if __name__ == "__main__":
    main()
