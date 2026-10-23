"""Guard the guard: break each Projects-thread-title rule in a temp copy of the repo
and confirm tests/test_project_threads.py goes red for every one.

    python scripts/falsify_project_threads.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
F = "project_threads.py"

# (file, original, mutant) - each must turn the suite red on its own.
MUTANTS = [
    (F, "        if off >= ln:\n", "        if True:\n"),                            # overlapping copy
    (F, "        if off == 0 or off > len(out):\n", "        if off == 0:\n"),
    (F, '            return self.raw(self.varint()).decode("utf-16-le", "replace")',
        '            return self.raw(self.varint()).decode("latin-1")'),
    (F, '        keys = {inp.get("message_id"), inp.get("thread_id")}', "        keys = set()"),
    (F, '                keys.update((j.get("thread_id"), j.get("session_id")))', "                pass"),
    (F, '            notes.setdefault(note_key(inp["instructions"]), set()).add((cid, title))', "            pass"),
    (F, "        resolved = {spawns.get(cid, t) if cid else t for cid, t in pairs}",
        "        resolved = {t for cid, t in pairs}"),                            # note ignores rename
    (F, '    return "note:" + " ".join(html.unescape(text).split())',
        '    return "note:" + text'),
    (F, "                    links[cmsg] = title.strip()", "                    pass"),
    (F, "    return {**links, **unique, **spawns}", "    return {**spawns, **unique, **links}"),
    (F, "        if len(resolved) == 1:", "        if resolved:"),                # ambiguous note
    (F, "        if not isinstance(x, (dict, list)) or id(x) in seen:", "        if not isinstance(x, (dict, list)):"),
    (F, "            stack.extend(reversed(list(x.values())))", "            stack.extend(x.values())"),
    (F, "            stack.extend(reversed(x))", "            stack.extend(x)"),
    (F, "    if data[1] == 0x11:", "    if True:"),                                  # uncompressed blob
    (F, "                        continue  # malformed, not a prompt", "                        break"),
    ("scanner.py", 'existing_source in ("ai", "prompt")', 'existing_source == "ai"'),
    (F, "                    keys.append(_MSG_ID.search(trig[0]).group(1))", "                    pass"),
    (F, """next((x for x in msgs if 'trigger="true"' in x[0]), msgs[-1] if msgs else None)""",
        "(msgs[0] if msgs else None)"),                                           # trigger message
    (F, "    if note:\n        keys.append(note)", "    if note:\n        keys.insert(0, note)"),  # ids first
    (F, '        renamed = source == "thread" and rank == _RANK["thread"] and title != current',
        "        renamed = False"),                                                # rename refresh
    (F, '        renamed = source == "thread" and rank == _RANK["thread"] and title != current',
        '        renamed = source == "thread" and rank == _RANK["thread"]'),          # rewrite unchanged
    (F, "                    keys.append(cse.group(1))", "                    pass"),
    (F, '                    body = " ".join(html.unescape(_TAGS.sub(" ", trig[1])).split())',
        '                    body = trig[1]'),
    (F, '_RANK = {"prompt": 1, "ai": 2, "thread": 3, "custom": 4}',
        '_RANK = {"prompt": 3, "ai": 2, "thread": 3, "custom": 4}'),
    (F, "        if not title or (_RANK[source] <= rank and not renamed):", "        if not title:"),
    (F, "        except Exception:  # cache files", "        except ValueError:  # cache files"),
    (F, "                    if r.get(\"type\") == \"user\" and not r.get(\"isMeta\"):\n"
        "                        break", "                    if False:\n                        break"),
]


def run(tree):
    # A mutant that drops cycle protection hangs rather than fails: a timeout counts as red.
    try:
        return subprocess.run([sys.executable, "-m", "unittest", "tests.test_project_threads"],
                              cwd=tree, capture_output=True, text=True, timeout=60).returncode
    except subprocess.TimeoutExpired:
        return 124


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db"))
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
