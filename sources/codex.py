"""sources/codex.py - OpenAI Codex CLI as an ingestion source.

Codex stores one rollout transcript per session as JSONL under
``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` (and rotated copies
under ``archived_sessions/``). Every line shares the envelope
``{timestamp, type, payload}``; token accounting lives only in
``event_msg`` records whose ``payload.type == "token_count"``.

This module produces the SAME ``(session_metas, turns, line_count)`` shape as the
vendored ``scanner.parse_jsonl_file`` so the multi-source engine writes Codex and
Claude through one path. Tokens are normalised into the existing column semantics
(see ``docs/SCHEMA.md``): Claude's ``input_tokens`` excludes cache, but Codex's
*includes* it — so we store ``input = input - cached`` and ``cache_read = cached``,
leaving the per-turn cost formula identical across agents. Codex has no
cache-creation concept (stored 0); reasoning tokens are kept informationally in
``reasoning_tokens`` (they're already inside ``output_tokens`` and not billed apart).

Three Codex-specific details, all verified against real ``~/.codex`` data:

1. **Stateful model attribution** — ``token_count`` carries no model; we track the
   latest ``turn_context.model`` and attribute usage to it.
2. **Delta tokens** — ``total_token_usage`` is the monotonic source of truth; the
   per-request delta is its advance since the previous record. Codex re-emits the
   same ``total`` on duplicate ``token_count`` events (different timestamps, no new
   usage), so summing ``last_token_usage`` would double-count — we diff the cumulative
   total instead and skip zero-delta re-emissions (and handle a backwards jump as a
   resume/compaction reset). ``last`` is only a fallback for older records that lack a
   total. Either way the file must be read in order, so Codex always parses whole-file.
3. **Synthetic dedup id** — Codex has no message id and branched/resumed sessions
   copy parent history verbatim into the child file. We synthesise a content-hash
   ``message_id`` (excluding session_id, so copies collapse) and route it through
   the existing partial-unique ``message_id`` index — making rescans idempotent.
"""

import glob
import json
import hashlib
from pathlib import Path

import policy
import scanner
from config import CODEX_PATH
from scanner import project_name_from_cwd

# Model used when a token_count appears before any turn_context names a model
# (early sessions had no model metadata). Flagged only by being a round family id.
FALLBACK_MODEL = "gpt-5"

NAME = "codex"


def discover_files(codex_path=None):
    """Return Codex rollout JSONL paths under sessions/ and archived_sessions/.

    Scoped deliberately to those two subtrees: ``~/.codex`` also holds unrelated
    overlay-tool cruft (pets/, *.sqlite, ambient-suggestions/) that is NOT Codex CLI
    session data and must never be parsed.
    """
    base = Path(codex_path or CODEX_PATH)
    files = []
    for sub in ("sessions", "archived_sessions"):
        d = base / sub
        if d.exists():
            files.extend(glob.glob(str(d / "**" / "rollout-*.jsonl"), recursive=True))
    files.sort()
    return files


def _int(d, *keys):
    """First present, coercible-to-int value among ``keys`` in dict ``d`` (else 0).

    Codex token field names drift across CLI versions (input_tokens / prompt_tokens
    / input), so accept the known aliases and tolerate stringified numbers.
    """
    if not isinstance(d, dict):
        return 0
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def _usage_fields(u):
    """Pull (input, cached, output, reasoning, total) from a usage sub-object."""
    inp = _int(u, "input_tokens", "prompt_tokens", "input")
    cached = _int(u, "cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
    out = _int(u, "output_tokens", "completion_tokens", "output")
    reason = _int(u, "reasoning_output_tokens", "reasoning_tokens")
    total = _int(u, "total_tokens")
    return inp, cached, out, reason, total


def _tool_call(p):
    """A Codex tool-call payload as the {name, input} dict Claude turns store.

    ``input`` stays a dict (waste.py and the dashboard read keys off it): a
    freeform ``custom_tool_call`` (e.g. ``exec``) keeps its raw text under
    ``command``; a ``function_call`` parses its JSON ``arguments``, falling back
    to the raw string under ``arguments``.
    """
    name = p.get("name")
    namespace = p.get("namespace")
    if isinstance(namespace, str) and namespace and isinstance(name, str) and name and not name.startswith(namespace + "."):
        name = namespace + "." + name
    if p.get("type") == "custom_tool_call":
        return {"name": name, "input": {"command": p.get("input") or ""}}
    args = p.get("arguments")
    try:
        inp = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        inp = None
    return {"name": name, "input": inp if isinstance(inp, dict) else {"arguments": args}}


def _synth_id(timestamp, model, inp, cached, out, reason, cumulative=None):
    """Deterministic dedup id from the usage content (NOT session id).

    Excluding session_id collapses branch/resume copies where Codex duplicates the
    parent's turns into a child file. The ms-precision timestamp + per-request token
    tuple keep genuinely distinct turns apart; ``cumulative`` (the session's running
    total *through* this record) is an extra cross-session discriminator that is still
    branch-copy-stable when copies replay full history from the start.
    """
    raw = f"{timestamp}|{model}|{inp}|{cached}|{out}|{reason}|{cumulative}"
    return "codex:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_file(filepath, start_line=0, strict=False):
    """Parse one Codex rollout file → (session_metas, turns, line_count).

    Note: ``start_line`` is accepted for interface symmetry with the Claude parser
    but Codex always parses whole-file — model attribution and delta recovery are
    stateful (order-dependent), and idempotent synthetic ids make a full re-parse on
    a grown file safe (duplicate turns are dropped by the unique index).

    ``strict`` re-raises read errors and malformed JSON instead of warning or
    skipping, so detail enrichment cannot record a partial read as complete.
    """
    session_meta = {}      # session_id -> meta dict
    turns = []
    current_model = None
    current_cwd = ""
    current_effort = ""   # reasoning effort, tracked statefully like current_model
    session_id = None
    prev_total = None      # cumulative usage seen so far (for delta fallback)
    seen_call_ids = set()  # replayed response_items must not duplicate tools
    pending_calls = []     # tool calls since the last turn; Codex writes them
                           # before the token_count of the response that made them
    line_count = 0

    # Fallback session id from the filename's trailing uuid, used only if a
    # token_count somehow precedes the session_meta line.
    fname_sid = Path(filepath).stem[-36:]

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if strict:
                        raise
                    continue
                if not isinstance(record, dict):
                    continue

                etype = record.get("type")
                ts = record.get("timestamp", "")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    payload = {}

                if etype == "session_meta":
                    sid = payload.get("id") or fname_sid
                    session_id = sid
                    cwd = payload.get("cwd", "") or ""
                    current_cwd = cwd
                    start_ts = payload.get("timestamp") or ts
                    git = payload.get("git")
                    session_meta.setdefault(sid, {
                        "session_id": sid,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": start_ts,
                        "last_timestamp": start_ts,
                        "git_branch": (git.get("branch") if isinstance(git, dict) else "") or "",
                        "model": None,
                        "agent": "codex",
                    })

                elif etype == "response_item" and payload.get("type") in (
                        "custom_tool_call", "function_call"):
                    call_id = payload.get("call_id")
                    if not isinstance(call_id, str):
                        call_id = None
                    if not call_id or call_id not in seen_call_ids:
                        pending_calls.append(_tool_call(payload))
                        if call_id:
                            seen_call_ids.add(call_id)

                elif etype == "turn_context":
                    m = payload.get("model")
                    if m:
                        current_model = m
                    c = payload.get("cwd")
                    if c:
                        current_cwd = c
                    e = payload.get("effort")
                    if e:
                        current_effort = e

                elif etype == "event_msg" and payload.get("type") == "thread_settings_applied":
                    # The other place Codex announces effort (as `reasoning_effort`).
                    # Last writer wins in file order, same as turn_context.
                    ts_settings = payload.get("thread_settings")
                    if isinstance(ts_settings, dict) and ts_settings.get("reasoning_effort"):
                        current_effort = ts_settings["reasoning_effort"]

                elif etype == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        # Older CLI (≤ ~0.106) emits token_count with null info.
                        continue

                    last = info.get("last_token_usage")
                    total_obj = info.get("total_token_usage")
                    if isinstance(total_obj, dict):
                        # `total_token_usage` is the monotonic source of truth (its
                        # running value telescopes to the session total). The
                        # per-request delta is its advance since the previous record;
                        # Codex re-emits the SAME total on duplicate token_count
                        # events (different timestamps, no new usage) — those give a
                        # zero delta and are skipped below, which is exactly what
                        # prevents double-counting. A backwards jump = cumulative
                        # reset (resume/compaction): the new total IS the delta.
                        ti, tc, to, tr, _ = _usage_fields(total_obj)
                        pi, pc, po, pr = prev_total or (0, 0, 0, 0)
                        inp    = ti - pi if ti >= pi else ti
                        cached = tc - pc if tc >= pc else tc
                        out    = to - po if to >= po else to
                        reason = tr - pr if tr >= pr else tr
                        prev_total = (ti, tc, to, tr)
                    elif isinstance(last, dict):
                        # Older/variant records without a cumulative total: fall back
                        # to the per-request delta and advance the running cumulative.
                        inp, cached, out, reason, _ = _usage_fields(last)
                        pi, pc, po, pr = prev_total or (0, 0, 0, 0)
                        prev_total = (pi + inp, pc + cached, po + out, pr + reason)
                    else:
                        continue

                    # Zero delta => a duplicate/no-op token_count re-emission (or an
                    # empty record); not a billable turn.
                    if inp + cached + out == 0:
                        continue

                    model = current_model or FALLBACK_MODEL
                    sid = session_id or fname_sid
                    fresh_input = max(inp - cached, 0)  # Codex input INCLUDES cached
                    calls, pending_calls = pending_calls, []

                    turns.append({
                        "session_id": sid,
                        "timestamp": ts,
                        "model": model,
                        "input_tokens": fresh_input,
                        "output_tokens": out,
                        "cache_read_tokens": cached,
                        # Codex reports no cache-creation tokens. CLI 0.146.0 added
                        # a `cache_write_input_tokens` field but has only ever sent
                        # 0; schema.py's KNOWN_CODEX_ZERO_ONLY_USAGE_KEYS canary
                        # fails `scan --doctor` the first time it is nonzero, which
                        # is when to decide the subtraction semantics and map it.
                        "cache_creation_tokens": 0,
                        "cache_creation_5m_tokens": 0,
                        "cache_creation_1h_tokens": 0,
                        "reasoning_tokens": reason,
                        "tool_name": calls[0]["name"] if calls else None,
                        "cwd": current_cwd,
                        "message_id": _synth_id(ts, model, inp, cached, out, reason, prev_total),
                        "duration_ms": 0,
                        "stop_reason": "",
                        "service_tier": "",
                        "inference_geo": "",
                        "is_sidechain": 0,
                        "is_compact_summary": 0,
                        "tool_calls": json.dumps(policy.redact_calls(calls)) if calls else None,
                        # Deliberately NOT part of _synth_id: adding it would change
                        # every historical id and re-insert the whole corpus.
                        "effort": current_effort,
                        "agent": "codex",
                    })

                    if sid in session_meta:
                        sm = session_meta[sid]
                        if ts and (not sm["last_timestamp"] or ts > sm["last_timestamp"]):
                            sm["last_timestamp"] = ts
                        if ts and (not sm["first_timestamp"] or ts < sm["first_timestamp"]):
                            sm["first_timestamp"] = ts
                        sm["model"] = model
    except Exception as e:  # noqa: BLE001 - match scanner's tolerant file handling
        if strict:
            raise
        print(f"  Warning: error reading {filepath}: {e}")

    # A file with token_count but no session_meta still yields turns; synthesise the
    # session meta from the filename uuid so the rollup has a home.
    if turns and not session_meta:
        sid = turns[0]["session_id"]
        session_meta[sid] = {
            "session_id": sid,
            "project_name": project_name_from_cwd(turns[0]["cwd"]),
            "first_timestamp": turns[0]["timestamp"],
            "last_timestamp": turns[-1]["timestamp"],
            "git_branch": "",
            "model": turns[-1]["model"],
            "agent": "codex",
        }

    return list(session_meta.values()), turns, line_count


class CodexSource:
    """Source adapter: discover + parse Codex rollout transcripts."""

    name = NAME

    def __init__(self, codex_path=None):
        self.codex_path = codex_path

    def discover_files(self):
        return discover_files(self.codex_path)

    def parse_file(self, filepath, start_line=0):
        metas, turns, line_count = parse_file(filepath, start_line=start_line)
        # 4th element: Codex has no subagent dispatch concept, so always empty.
        return metas, turns, line_count, []

    def backfill_effort(self, conn):
        """One-time turns.effort backfill (see scanner.backfill_effort_once).

        No line prefilter is possible here — Codex effort is stateful (announced
        in an earlier turn_context / thread_settings record), so recovering a
        turn's effort means replaying the file. Rollouts are few and this runs
        once, so a full re-parse is fine. Synthetic message_ids are deterministic,
        so they match the rows already in the DB.
        """
        files = discover_files(self.codex_path)
        if not files:
            return 0  # nothing discovered: leave the marker unset and retry later

        def pairs():
            for filepath in files:
                _, turns, _ = parse_file(filepath)
                for t in turns:
                    yield t["message_id"], t["effort"]

        return scanner.backfill_effort_once(conn, "effort_backfill_done_codex", pairs)

    def backfill_details(self, conn):
        """Fill missing details without changing usage, IDs, or existing metadata.

        Receipts cover individual, completely read file versions, never the whole
        corpus: absent transcripts can return later and failed/partial reads retry.
        A receipt contains mtime_ns and size, so unchanged files need only a stat.
        The old global completion marker is deliberately ignored.
        """
        key = "detail_backfill_files_codex"
        raw = scanner._meta_get(conn, key)
        try:
            receipts = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            receipts = {}
        if not isinstance(receipts, dict):
            receipts = {}
        before = conn.total_changes
        receipt_changed = False
        for filepath in discover_files(self.codex_path):
            try:
                stat = Path(filepath).stat()
                fingerprint = [stat.st_mtime_ns, stat.st_size]
                if receipts.get(filepath) == fingerprint:
                    continue
                metas, turns, _ = parse_file(filepath, strict=True)
                after = Path(filepath).stat()
                if fingerprint != [after.st_mtime_ns, after.st_size]:
                    # A rollout may grow while it is read. Retry a stable version.
                    continue
            except Exception as e:  # noqa: BLE001 - retry this file next scan
                print(f"  Warning: Codex detail backfill deferred ({filepath}: {e})")
                continue
            # Do not receipt a fresh file until its rows exist in the database:
            # this hook runs before the normal ingestion walk.
            if not any(conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ? AND agent = 'codex'",
                    (m["session_id"],)).fetchone() for m in metas):
                continue
            calls = [(t["message_id"], t["tool_name"], t["tool_calls"])
                     for t in turns if t["tool_calls"]]
            conn.executemany(scanner.backfill_column_sql("tool_name") + " AND agent = 'codex'",
                             ((n, mid) for mid, n, _ in calls))
            conn.executemany(scanner.backfill_column_sql("tool_calls") + " AND agent = 'codex'",
                             ((c, mid) for mid, _, c in calls))
            conn.executemany(
                "UPDATE sessions SET git_branch = ? WHERE session_id = ? AND agent = 'codex' "
                "AND (git_branch IS NULL OR git_branch = '')",
                ((m["git_branch"], m["session_id"]) for m in metas if m["git_branch"]))
            receipts[filepath] = fingerprint
            receipt_changed = True
        if receipt_changed:
            scanner._meta_set(conn, key, json.dumps(receipts, sort_keys=True))
        conn.commit()
        return conn.total_changes - before

    def sync_titles(self, conn):
        """Set Codex session topics from ``session_index.jsonl`` (thread_name).

        Runs after the walk on every scan, like ClaudeSource.sync_agents: a rename
        appends to the index without touching the rollout, so a one-shot or
        parse-time read would keep the old name. Latest entry per id wins. The
        index is one short line per thread, so re-reading it is cheap.
        """
        path = Path(self.codex_path or CODEX_PATH) / "session_index.jsonl"
        names = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(r, dict) and r.get("id") and r.get("thread_name"):
                        names[r["id"]] = r["thread_name"]
        except OSError:
            return 0
        before = conn.total_changes
        conn.executemany(
            "UPDATE sessions SET topic = ?, topic_source = 'ai' "
            "WHERE session_id = ? AND agent = 'codex' "
            "AND (topic IS NULL OR topic = '' OR topic_source = 'ai') "
            "AND topic IS NOT ?",
            ((name, sid, name) for sid, name in names.items()))
        conn.commit()
        return conn.total_changes - before
