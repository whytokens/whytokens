"""Guard the guard: break each provenance rule (vocabulary, observed/inferred split, unknown-not-zero,
badged headers, prefilled verify hint) in a temp copy of the repo and confirm test_provenance goes red.

    python scripts/falsify_provenance.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn tests.test_provenance red on its own.
MUTANTS = [
    ("provenance.py", '"unknown": "Not measurable', '"unknownx": "Not measurable'),
    ("provenance.py", 'return "inferred" if tier == "inferred" else "observed"', 'return "observed"'),
    ("waste.py", '"attribution": provenance.attribution(tier), ', ""),
    ("api.py", 'r[f["attribution"] + "_usd"] += f["usd"]', 'r["observed_usd"] += f["usd"]'),
    ("api.py", "self._send_json(provenance.LABELS)", "self._send_json({})"),
    ("static/modules/provenance.js", "acc[f.attribution] += f.usd", "acc.observed += f.usd"),
    ("static/modules/provenance.js", "return unknown || value == null ? '\u2014' : format(value);", "return format(value || 0);"),
    ("static/modules/provenance.js", "title=\"${esc(LABELS[kind] || kind)}\"", 'title=""'),
    ("static/modules/verify.js", "--cause ${cause} --note", "--cause X --note"),
    ("static/index.html", """<th data-prov="local_estimate" class="sortable" onclick="setSessionSort('cost')">""",
     """<th class="sortable" onclick="setSessionSort('cost')">"""),
    ("static/index.html", """<th data-prov="official" class="sortable" onclick="setSessionSort('cache_read')">""",
     """<th data-prov="local_estimate" class="sortable" onclick="setSessionSort('cache_read')">"""),
]

TESTS = ["tests.test_provenance"]


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
