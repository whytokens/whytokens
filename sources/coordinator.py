"""Claude Projects coordinator spend: a PARTIAL source.

The cloud coordinator that spawns Projects threads runs on claude.ai and writes no
local transcript. Its events are cached by the desktop app in the same claude.ai
IndexedDB blobs that ``project_threads`` decodes for thread titles, and each
assistant message there carries ``usage``. The cache is a rolling window (about 200
events per coordinator), so this source sees only what was cached while scans ran.
Turns persist once captured, so totals are a lower bound, never the full bill.

Tokens come from the ``result`` event that closes each coordinator query, not the
cached assistant messages: those are mid-stream snapshots whose output tallies are
far below final. A result's ``usage`` is that query's main-model total (with the
1h/5m cache-write split). Its ``modelUsage`` is cumulative per model since the
conversation began, so a side model's (e.g. Haiku's) share of the query is the
delta from the previous result. Ids are the result's event uuid, stable across
scans. Session id is the coordinator's ``cse_`` conversation id.
"""

from pathlib import Path

import project_threads

NAME = "coordinator"
PROJECT = "Projects coordinator (partial: cached window)"


def _n(x):
    """A token count, or 0 for anything malformed (the cache is undocumented)."""
    return x if isinstance(x, int) and not isinstance(x, bool) and x > 0 else 0


def _s(x):
    return x if isinstance(x, str) else ""


def _turn(sid, ts, mid, model, inp, out, cr, cw, cw5, cw1, speed="", geo="", tier=""):
    return {
        "session_id": sid, "timestamp": ts, "model": model, "message_id": mid,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": cr, "cache_creation_tokens": cw,
        "cache_creation_5m_tokens": cw5, "cache_creation_1h_tokens": cw1,
        "tool_name": None, "cwd": "", "duration_ms": 0, "stop_reason": "end_turn",
        "service_tier": tier, "inference_geo": geo, "speed": speed,
        "is_sidechain": 0, "is_compact_summary": 0, "agent": NAME,
    }


def _model(mu, k):
    if k is None:
        return ""
    return _s((mu.get(k) or {}).get("canonicalModel")) or k.split("[")[0]


_MU = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def parse_value(v):
    """(session_meta, turns) for one decoded cache value; (None, []) if it isn't a
    cached Code conversation."""
    if not isinstance(v, dict) or not str(v.get("conversationUuid", "")).startswith("code:"):
        return None, []
    sid = v["conversationUuid"][len("code:"):]
    results = sorted(
        (d for d in project_threads._walk(v.get("tree"))
         if d.get("type") == "result" and isinstance(d.get("usage"), dict) and d.get("uuid")),
        key=lambda d: str(d.get("created_at") or ""))
    turns, prev = [], None
    for r in results:
        u, mu = r["usage"], r.get("modelUsage") if isinstance(r.get("modelUsage"), dict) else {}
        ts = str(r.get("created_at") or "")
        ts = ts[:23] + "Z" if len(ts) > 23 else ts  # transcripts' millisecond form
        # This query's share per model: the delta from the previous result (the
        # first result in the window has no base, so its cumulative stands in).
        delta = {}
        for k, m in mu.items():
            if isinstance(m, dict):
                base = (prev or {}).get(k) if isinstance((prev or {}).get(k), dict) else {}
                delta[k] = [max(_n(m.get(f)) - _n(base.get(f)), 0) for f in _MU]
        # The main model ran the loop and takes the exact per-query usage. It is the
        # model whose delta equals that usage (picked per query: a conversation can
        # switch model). With no base, or no exact match, the largest delta is a
        # guess: the row is marked estimated so a later blob that confirms the model
        # replaces it (scanner.insert_turns lets a real row overwrite an estimate).
        own = [_n(u.get(f)) for f in ("input_tokens", "output_tokens",
                                       "cache_read_input_tokens", "cache_creation_input_tokens")]
        # Only a predecessor can confirm: without one, a model's cumulative may equal
        # this query's usage merely because it is that model's first query, while the
        # other models' cumulatives are older history, not this query's side calls.
        exact = [k for k, d in delta.items() if d == own] if prev is not None else []
        main = exact[0] if len(exact) == 1 else max(
            delta, key=lambda k: (delta[k][2], delta[k][0] + delta[k][3], delta[k][1]), default=None)
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        t = _turn(sid, ts, r["uuid"], _model(mu, main),
                  _n(u.get("input_tokens")), _n(u.get("output_tokens")),
                  _n(u.get("cache_read_input_tokens")), _n(u.get("cache_creation_input_tokens")),
                  _n(cc.get("ephemeral_5m_input_tokens")), _n(cc.get("ephemeral_1h_input_tokens")),
                  _s(u.get("speed")), _s(u.get("inference_geo")), _s(u.get("service_tier")))
        t["estimated"] = 0 if len(exact) == 1 else 1
        if t["input_tokens"] or t["output_tokens"] or t["cache_read_tokens"] or t["cache_creation_tokens"]:
            turns.append(t)
        # Side models, only once the main model is confirmed (else their share is
        # unknown). modelUsage has no TTL split, so their cache writes are priced as
        # 5m (side calls rarely cache).
        if len(exact) == 1:
            for k, d in delta.items():
                if k != main and any(d):
                    turns.append(_turn(sid, ts, f"{r['uuid']}:{_model(mu, k)}", _model(mu, k),
                                       d[0], d[1], d[2], d[3], d[3], 0))
        prev = mu
    if not turns:
        return None, []
    meta = {
        "session_id": sid,
        "project_name": PROJECT,
        "first_timestamp": turns[0]["timestamp"],
        "last_timestamp": turns[-1]["timestamp"],
        "git_branch": "",
        "model": turns[0]["model"],
        "agent": NAME,
    }
    return meta, turns


def fix_bounds(conn):
    """Recompute coordinator sessions' first/last timestamps from their turns.
    Overlapping blobs of one conversation can arrive newest first, and a session's
    first insert would otherwise fix its start at the later window's."""
    conn.execute("""
        UPDATE sessions SET
            first_timestamp = (SELECT MIN(timestamp) FROM turns WHERE turns.session_id = sessions.session_id),
            last_timestamp  = (SELECT MAX(timestamp) FROM turns WHERE turns.session_id = sessions.session_id)
        WHERE project_name = ?
          AND EXISTS (SELECT 1 FROM turns WHERE turns.session_id = sessions.session_id)
    """, (PROJECT,))


class CoordinatorSource:
    name = NAME

    def __init__(self, blob_dir=None):
        self.blob_dir = blob_dir

    def discover_files(self):
        import config  # read at discover time so tests can patch it
        root = self.blob_dir or config.desktop_cache_dir()  # None = not opted in
        if root is None:
            return []
        root = Path(root)
        try:
            return [str(p) for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
        except OSError:
            return []

    def reconcile(self, conn):
        fix_bounds(conn)
        conn.commit()

    def parse_file(self, filepath, start_line=0):
        # Chromium writes each update to a new, higher-numbered blob file, so a file
        # is parsed once whole; start_line is unused. line_count must still be >0
        # for the walk to record the file as processed.
        try:
            with open(filepath, "rb") as f:
                meta, turns = parse_value(project_threads.decode_blob(f.read()))
        except Exception:  # private cache, rewritten under us; never fatal
            return [], [], 1, []
        return ([meta] if meta else []), turns, max(len(turns), 1), []
