"""Hand-label sessions as "went fine" or "needed rework", then see which rework signal separates them.

    python scripts/label_rework.py            # label ~30 sessions (resumable; q to stop)
    python scripts/label_rework.py --report   # how D1-D5 split your labels (rework.separation)
    python scripts/label_rework.py --n 40

Labelling is blind: the rework signals are never shown while you label, only what helps you
recall the session (date, project, title, size, and the first and last prompt from its
transcript). Sessions are drawn round-robin across work shapes, only ones whose transcript is
still on disk and that did not stop on a usage limit. Everything stays local: labels are written
to <whytokens data dir>/rework-labels.json. Nothing is sent anywhere.
"""

import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import rework  # noqa: E402
import scanner  # noqa: E402

LABELS = config.DATA_DIR / "rework-labels.json"
KEYS = {"f": "fine", "r": "rework"}


def load(path=LABELS):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(labels, path=LABELS):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(labels, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def pick(measures, labelled, n, seed=0):
    """Up to n unlabelled measurable sessions, round-robin across shapes (stable for a seed)."""
    by_shape = defaultdict(list)
    for sid, m in sorted(measures.items()):
        if sid not in labelled and m["limit_hit"] is False and m["prompts"] >= 2:
            by_shape[m["shape"] or "unknown"].append(sid)
    rng = random.Random(seed)
    for v in by_shape.values():
        rng.shuffle(v)
    out, shapes = [], sorted(by_shape)
    while len(out) < n and any(by_shape.values()):
        for s in shapes:
            if by_shape[s] and len(out) < n:
                out.append(by_shape[s].pop())
    return out


def prompts_of(path):
    first = last = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if scanner.is_user_prompt(r):
                    text = " ".join(rework._text((r.get("message") or {}).get("content")).split())
                    first = first or text
                    last = text
    except OSError:
        pass
    return first[:300], last[:300]


def card(conn, sid, m):
    s = conn.execute("SELECT first_timestamp, last_timestamp, topic, git_branch, turn_count FROM sessions "
                     "WHERE session_id = ?", (sid,)).fetchone()
    path = rework._transcript(conn, sid)
    first, last = prompts_of(path) if path else ("", "")
    return (f"\n{s[0][:16]} -> {s[1][11:16]}  {m['project']}  branch {s[3] or '-'}  shape {m['shape'] or '-'}\n"
            f"title: {s[2] or '(none)'}\n{m['prompts']} prompts, {s[4]} turns\n"
            f"first prompt: {first or '(not found)'}\nlast prompt:  {last or '(not found)'}")


def report(measures, labels):
    rows = rework.separation(measures, labels)
    print(f"{len(labels)} labels. AUC: 1.0 separates perfectly, 0.5 is chance (rework-signal-spec.md gate 2).")
    print(f"{'signal':<16}{'fine':>6}{'rework':>8}{'mean fine':>12}{'mean rework':>13}{'AUC':>7}")
    for sig, r in rows.items():
        f = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
        print(f"{sig:<16}{r['n_fine']:>6}{r['n_rework']:>8}{f(r['mean_fine']):>12}{f(r['mean_rework']):>13}{f(r['auc']):>7}")


def main(argv=None, ask=input, db_path=None, labels_path=LABELS):
    argv = sys.argv[1:] if argv is None else argv
    db_path = db_path or config.DB_PATH
    measures = rework.measure(db_path)
    labels = load(labels_path)
    if "--report" in argv:
        report(measures, labels)
        return
    n = int(argv[argv.index("--n") + 1]) if "--n" in argv else 30
    todo = pick(measures, labels, max(0, n - len(labels)))
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        for i, sid in enumerate(todo, 1):
            print(card(conn, sid, measures[sid]))
            a = ask(f"[{len(labels) + 1}/{n}] f = went fine, r = needed rework, s = skip, q = quit: ").strip().lower()
            if a == "q":
                break
            if a in KEYS:
                labels[sid] = KEYS[a]
                save(labels, labels_path)
    finally:
        conn.close()
    print(f"\n{len(labels)} sessions labelled in {labels_path}. Run with --report to see the separation.")


if __name__ == "__main__":
    main()
