"""Break each .zst-reading rule in sources/codex.py and check tests/test_codex_zst.py goes red.

Run: python scripts/falsify_codex_zst.py   (restores the file; exit 1 if a mutant survives)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sources" / "codex.py"

MUTANTS = {
    "drop .zst discovery": ("    if zst_decoder():\n        files = sorted(", "    if False:\n        files = sorted("),
    "id depends on file form": ("reason, prev_total),", "reason, str(prev_total) + Path(filepath).suffix),"),
    "unread count silent": ('return [] if zst_decoder() else _find(codex_path, "rollout-*" + ZST)', "return []"),
}


def red():
    r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_codex_zst"],
                       cwd=ROOT, capture_output=True)
    return r.returncode != 0


def main():
    original = SRC.read_bytes()
    text = original.decode("utf-8").replace("\r\n", "\n")
    bad = []
    try:
        if red():
            print("baseline is already red"); return 1
        for name, (old, new) in MUTANTS.items():
            if old not in text:
                print(f"mutant '{name}' no longer applies"); bad.append(name); continue
            SRC.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            ok = red()
            print(f"{'caught  ' if ok else 'SURVIVED'} {name}")
            if not ok:
                bad.append(name)
    finally:
        SRC.write_bytes(original)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
