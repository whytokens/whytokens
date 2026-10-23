"""Falsification harness for tests/test_live.py.

A test suite that never fails enforces nothing, and /api/live's output feeds a
cost warning that gets acted on - so the suite has to be shown catching each
defect it claims to. This mutates api.py one defect at a time and asserts the
suite goes RED for every one, then restores the file.

    python tests/falsify_live.py     # exits non-zero if any mutant survives
"""

import shutil
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "api.py"
BAK = API.with_suffix(".py.falsifybak")

# (name, original source, mutation) - each is a way get_live_session could be
# wrong while still returning a plausible-looking dollar figure.
MUTANTS = [
    ("drop subagent transcripts",
     'return session_id, [main] + subs',
     'return session_id, [main]'),
    ("price every turn at one model",
     't.get("model") or "unknown", t.get("input_tokens", 0)',
     '"claude-haiku-4-5-20251001", t.get("input_tokens", 0)'),
    ("count context from any turn, incl. sidechains",
     'main = [t for t in turns if not t.get("is_sidechain")] or turns',
     'main = turns'),
    ("mangle only the path separators in a cwd slug",
     'slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)',
     'slug = re.sub(r"[:\\\\/]", "-", cwd)'),
    ("let any session_id reach the filesystem glob",
     'if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id):\n            return "", []',
     'if False:\n            return "", []'),
    ("ignore WHYTOKENS_EXTRA_PROJECTS_DIRS",
     'config.claude_projects_dirs(scanner.DEFAULT_PROJECTS_DIRS)',
     'scanner.DEFAULT_PROJECTS_DIRS'),
    ("treat cache writes as free",
     't.get("cache_creation_tokens", 0),\n        t.get("timestamp")',
     '0,\n        t.get("timestamp")'),
    ("label the session by its lowest-priority model",
     'model = max(votes, key=',
     'model = min(votes, key='),
    ("let subagent turns vote on the session model",
     'if t.get("model") and not t.get("is_sidechain")',
     'if t.get("model")'),
    ("let advisor turns vote on the session model",
     'and not (t.get("message_id") or "").startswith(scanner.ADVISOR_ID_PREFIX))',
     ')'),
]


def main():
    src = API.read_text(encoding="utf-8")
    shutil.copy(API, BAK)
    survived = 0
    try:
        for name, old, new in MUTANTS:
            if old not in src:
                print("SKIP   %s   <-- mutation target no longer in api.py" % name)
                survived += 1
                continue
            API.write_text(src.replace(old, new, 1), encoding="utf-8")
            r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_live"],
                               cwd=str(API.parent), capture_output=True, text=True)
            caught = r.returncode != 0
            survived += not caught
            print(("RED    " if caught else "GREEN  ") + name +
                  ("" if caught else "   <-- NOT caught by tests/test_live.py"))
    finally:
        shutil.copy(BAK, API)
        BAK.unlink()
    print("\nall mutants caught" if not survived else
          "\n%d mutant(s) survived" % survived)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
