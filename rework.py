"""Candidate rework signals D1-D5 (docs/private/rework-signal-spec.md): measurements only.

Nothing here feeds verify, the dashboard, waste or any total. These are per-session numbers kept
so the owner's hand labels (scripts/label_rework.py) can test whether any of them separates
sessions that "went fine" from ones that "needed rework". Until one passes that gate, none ships.

Claude sessions only. Each rate is per genuine user prompt. ``None`` means unmeasured,
never zero:

- D1 ``error_rate``: failed tool calls / prompts. Unmeasured while any call lacks an id or a result
  row, or its is_error is NULL.
- D2 ``churn_rate``: edits to a file already edited under an earlier prompt / prompts. File level:
  the DB keeps the path, not the edited region. Unmeasured when a main-thread edit has no prompt ordinal.
- D3 ``interrupt_rate``: "[Request interrupted" user records / prompts. Read from the transcript
  (the DB does not store them); unmeasured once the transcript is gone.
- D4 ``followups``: prompts per request minus 1; a request is a run of prompts with no gap over
  ``GAP_MIN`` minutes between them, timed by the prompt records (transcript only).
- D5 ``restarted``: 1 when the session was short (<= ``SHORT_PROMPTS`` prompts) and another session
  in the same project and branch began within ``GAP_MIN`` minutes after its end.

``limit_hit``: the transcript shows an API rate- or usage-limit stop. Such sessions are excluded
from the scorer, never counted as rework (spec gate 4). Unknown (None) once the transcript is gone.
"""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import scanner

GAP_MIN = 30
SHORT_PROMPTS = 3
SIGNALS = ("error_rate", "churn_rate", "interrupt_rate", "followups", "restarted")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except (AttributeError, ValueError):
        return None


def _transcripts(conn, sid):
    """Every scanned copy of the session's main transcript still on disk (exact <sid>.jsonl
    basename), largest first: a stale row for a moved file does not hide another copy."""
    name = f"{sid}.jsonl"
    found = [Path(path) for (path,) in conn.execute(
        "SELECT path FROM processed_files WHERE path LIKE ? AND source = 'claude'", (f"%{name}",))]
    found = [p for p in found if p.name == name and p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_size, reverse=True)


def _transcript(conn, sid):
    copies = _transcripts(conn, sid)
    return copies[0] if copies else None


def _marks(conn, sid):
    """Transcript marks across every copy: prompts, times and interrupts from the copy with the
    most prompts; a limit stop seen in any copy counts. None when copies disagree on prompts in a
    way neither explains (two copies, same prompt count, different times) or none is readable."""
    reads = [m for m in (transcript_marks(p) for p in _transcripts(conn, sid)) if m is not None]
    if not reads:
        return None
    best = max(reads, key=lambda m: len(m["prompt_times"]))
    if any(len(m["prompt_times"]) == len(best["prompt_times"]) and m["prompt_times"] != best["prompt_times"]
           for m in reads):
        return None
    return {**best, "limit_hit": any(m["limit_hit"] for m in reads)}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def transcript_marks(path):
    """{prompt_times, interrupts, limit_hit} read from one transcript, or None when unreadable.
    Prompts are the genuine user prompts (scanner.is_user_prompt, the rule behind prompt_seq),
    timed by the prompt record itself, not by the reply."""
    times, interrupts, limit = [], 0, False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                msg = r.get("message") if isinstance(r.get("message"), dict) else {}
                if scanner.is_user_prompt(r):
                    times.append(_ts(r.get("timestamp")))
                elif r.get("type") == "user" and _text(msg.get("content")).startswith("[Request interrupted"):
                    interrupts += 1
                elif r.get("type") == "assistant" and msg.get("model") == "<synthetic>" and (
                        r.get("error") == "rate_limit" or "usage limit" in _text(msg.get("content")).lower()):
                    limit = True
    except OSError:
        return None
    return {"prompt_times": times, "interrupts": interrupts, "limit_hit": limit}


def _requests(times):
    """Runs of prompts with no gap over GAP_MIN; None when any prompt has no usable timestamp."""
    if not times or any(t is None for t in times):
        return None
    return 1 + sum((b - a).total_seconds() > GAP_MIN * 60 for a, b in zip(times, times[1:]))


def measure(db_path, read_transcripts=True):
    """{session_id: {prompts, shape, project, error_rate, churn_rate, interrupt_rate, followups,
    restarted, limit_hit}} for every Claude session with at least one prompt.

    With the transcript on disk, prompts and their times come from it. Without it, prompts is the
    highest turns.prompt_seq (a prompt nobody answered at the very end is missed) and the
    transcript-only signals (D3, D4, limit_hit) are None."""
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions = {r["session_id"]: r for r in conn.execute(
            "SELECT session_id, project_name, git_branch, first_timestamp, last_timestamp, shape "
            "FROM sessions WHERE agent = 'claude'")}
        max_seq, edits, call_ids = defaultdict(int), defaultdict(list), defaultdict(set)
        no_id, unordered = set(), set()  # sessions where D1 / D2 cannot be complete
        for r in conn.execute("SELECT session_id, prompt_seq, tool_calls, is_sidechain FROM turns "
                              "WHERE agent = 'claude' ORDER BY timestamp"):
            sid, seq = r["session_id"], r["prompt_seq"]
            try:
                calls = json.loads(r["tool_calls"] or "[]")
            except ValueError:
                calls = []
            for c in calls if isinstance(calls, list) else []:
                if not isinstance(c, dict):
                    continue
                if c.get("id"):
                    call_ids[sid].add(c["id"])
                else:
                    no_id.add(sid)  # a call that cannot be matched to its result
                key = EDIT_TOOLS.get(c.get("name"))
                inp = c.get("input") if key and isinstance(c.get("input"), dict) else {}
                if inp.get(key) and not r["is_sidechain"]:
                    if seq:
                        edits[sid].append((seq, inp[key]))
                    else:
                        unordered.add(sid)  # a main-thread edit with no prompt ordinal
            if seq:
                max_seq[sid] = max(max_seq[sid], seq)
        results = defaultdict(dict)  # session -> call_id -> is_error
        for r in conn.execute("SELECT session_id, call_id, is_error FROM tool_results"):
            results[r["session_id"]][r["call_id"]] = r["is_error"]
        out = {}
        for sid, s in sessions.items():
            marks = _marks(conn, sid) if read_transcripts else None
            limit = marks["limit_hit"] if marks else None
            if marks and len(marks["prompt_times"]) < max_seq[sid]:
                # The surviving copy is shorter than what the DB saw: transcript-only signals are
                # unknown, and so is the stop unless this copy positively shows one.
                marks, limit = None, (True if limit else None)
            prompts = len(marks["prompt_times"]) if marks else max_seq[sid]
            if not prompts:
                continue
            res = results[sid]
            # D1 is measured only when every call the DB knows of has a result with a known flag.
            known = call_ids[sid] | set(res)
            measured = sid not in no_id and all(res.get(cid) is not None for cid in known)
            seen, churn = {}, 0
            for seq, path in edits[sid]:
                if seen.setdefault(path, seq) < seq:
                    churn += 1
            requests = _requests(marks["prompt_times"]) if marks else None
            out[sid] = {"prompts": prompts, "shape": s["shape"], "project": s["project_name"],
                        "error_rate": sum(1 for v in res.values() if v) / prompts if measured else None,
                        "churn_rate": None if sid in unordered else churn / prompts,
                        "interrupt_rate": marks["interrupts"] / prompts if marks else None,
                        "followups": prompts / requests - 1 if requests else None,
                        "restarted": 0, "limit_hit": limit}
        _restarts(sessions, out)
        return out
    finally:
        conn.close()


def _restarts(sessions, out):
    """D5: a short session counts as restarted when the first other session in the same project
    and branch that starts at or after its end starts within GAP_MIN of it (overlaps skipped)."""
    by_key = defaultdict(list)
    for sid in out:
        s = sessions[sid]
        by_key[(s["project_name"], s["git_branch"])].append(sid)
    for sids in by_key.values():
        starts = sorted((_ts(sessions[x]["first_timestamp"]), x) for x in sids if _ts(sessions[x]["first_timestamp"]))
        for a in sids:
            end = _ts(sessions[a]["last_timestamp"])
            if out[a]["prompts"] > SHORT_PROMPTS or end is None:
                continue
            nxt = next((t for t, b in starts if b != a and t >= end), None)
            if nxt is not None and (nxt - end).total_seconds() <= GAP_MIN * 60:
                out[a]["restarted"] = 1


def auc(rework, fine):
    """P(a rework session scores above a fine one), ties count half: 1.0 separates perfectly,
    0.5 is chance, below 0.5 points the wrong way. None without both groups."""
    if not rework or not fine:
        return None
    wins = sum((r > f) + 0.5 * (r == f) for r in rework for f in fine)
    return wins / (len(rework) * len(fine))


def separation(measures, labels):
    """Per signal: how the owner's labels split it. Sessions with a limit stop, or unknown limit
    status, are excluded; an unmeasured value leaves that session out of that signal only."""
    rows = {}
    for sig in SIGNALS:
        groups = {"fine": [], "rework": []}
        for sid, lab in labels.items():
            m = measures.get(sid)
            if m is None or m["limit_hit"] is not False or lab not in groups or m[sig] is None:
                continue
            groups[lab].append(m[sig])
        mean = {k: (sum(v) / len(v) if v else None) for k, v in groups.items()}
        rows[sig] = {"n_fine": len(groups["fine"]), "n_rework": len(groups["rework"]),
                     "mean_fine": mean["fine"], "mean_rework": mean["rework"],
                     "auc": auc(groups["rework"], groups["fine"])}
    return rows
