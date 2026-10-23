"""
schema.py - Schema-drift armor for the Claude Code transcript envelope.

The transcript envelope is **not officially documented** and drifts between Claude
Code versions; only the ``usage`` sub-object is a stable (Anthropic API) schema.
Parsing in ``scanner.py`` is defensive (``.get()`` everywhere), so the real risk is
not a crash — it's silently *missing* a newly-added field. This module owns the
*known surface*: the allowlisted set of keys we recognise at each level of a record,
plus the set of Claude Code ``version`` strings seen when this allowlist was last
refreshed.

It is the single source for both:

  * the **schema-surface canary** (``tests/test_schema.py``) — asserts the golden
    corpus contains nothing outside these allowlists, so a refreshed fixture with a
    new key turns the build red; and
  * ``scan --doctor`` (``cli.py``) — reports unknown keys / unseen versions found in
    the *real* logs, so drift in the wild is visible without waiting for a crash.

Everything here is **read-only**: it never writes the DB or changes parsing. The
vendored ``scanner.py`` stays untouched; drift *detection* lives in this separate
module by design.
"""

import json
import glob
from pathlib import Path


# ── Allowlists ───────────────────────────────────────────────────────────────────
# Captured from the user's real ~/.claude/projects logs. These are the *recognised*
# keys; the doctor flags anything outside them. Many top-level keys belong to record
# types the scanner intentionally ignores (titles, queue ops, snapshots) — they are
# listed so the doctor doesn't cry wolf over fields we already know about.

# Top-level record `type` values we have seen. Only "assistant" / "user" are
# ingested; the rest are recognised-and-ignored. A genuinely new type is drift.
# "started" / "result" are Workflow journal bookkeeping (subagents/workflows/
# */journal.jsonl: {type, key, agentId[, result]}); "frame-link" maps a local
# scratchpad file to a published claude.ai artifact URL. 2.1.207+ additions:
# "agent-name" ({type, agentName, sessionId}) records a session's display name;
# "relocated" ({type, sessionId, relocatedCwd}) records a session's working
# directory moving. Neither carries usage, and project attribution still comes
# from the per-turn `cwd`, so "relocated" needs no parsing. None carry usage.
KNOWN_RECORD_TYPES = frozenset({
    "assistant", "user", "system", "attachment", "queue-operation",
    "ai-title", "custom-title", "last-prompt", "mode", "permission-mode",
    "file-history-snapshot", "started", "result", "frame-link",
    "agent-name", "relocated",
})

# Top-level keys across all record types. Includes the API-error/retry envelope
# (error, cause, maxRetries, retryAttempt, retryInMs, isApiErrorMessage,
# apiErrorStatus, apiError, errorDetails) that wraps failed turns. 2.1.170+
# additions: advisorModel + session_id (snake_case twin of sessionId) on every
# assistant record; toolEndsTurn / toolDenialKind / classifierMetaLines on user
# tool_result records; key / result (workflow journal); path / frameUrl
# (frame-link). 2.1.215+ additions: effort (the output_config effort level of the
# turn, on assistant records — ingested into turns.effort, which drives the
# Cost by Effort Level view); source (on system/api_error retry envelopes, e.g.
# "request_retry"); title (on frame-link, the published artifact's name);
# agentName / relocatedCwd (the payloads of the two new record types above).
# All metadata — none carry tokens.
KNOWN_RECORD_KEYS = frozenset({
    "advisorModel", "agentId", "agentName", "aiTitle", "apiError", "apiErrorStatus",
    "attachment", "attributionAgent",
    "attributionMcpServer", "attributionMcpTool", "attributionPlugin",
    "attributionSkill", "cause", "compactMetadata", "content", "customTitle",
    "classifierMetaLines", "cwd", "data", "durationMs", "effort", "entrypoint",
    "error", "errorDetails", "frameUrl", "gitBranch", "hasOutput",
    "hookAdditionalContext", "hookCount", "hookErrors", "hookInfos",
    "isApiErrorMessage", "isCompactSummary",
    "isMeta", "isSidechain", "isSnapshotUpdate", "isVisibleInTranscriptOnly",
    "key", "lastPrompt", "leafUuid", "level", "logicalParentUuid", "maxRetries",
    "mcpMeta", "message", "messageCount", "messageId", "mode", "operation",
    "origin", "parentUuid", "path", "permissionMode", "preventedContinuation",
    "promptId", "promptSource", "relocatedCwd", "requestId", "result",
    "retryAttempt", "retryInMs", "sessionId", "session_id", "slug",
    "snapshot", "source", "sourceToolAssistantUUID", "sourceToolUseID",
    "stopReason", "subtype", "timestamp", "title", "toolDenialKind",
    "toolEndsTurn", "toolUseID",
    "toolUseResult", "type", "userType", "uuid", "version",
})

# Keys of the assistant ``message`` object. ``container`` and ``context_management``
# are recent additions (server-side context editing / container execution).
KNOWN_MESSAGE_KEYS = frozenset({
    "container", "content", "context_management", "diagnostics", "id", "model",
    "role", "stop_details", "stop_reason", "stop_sequence", "type", "usage",
})

# Keys of ``message.usage`` — the token-accounting surface. This is the highest-value
# canary: a new token bucket here is money we'd silently stop counting.
KNOWN_USAGE_KEYS = frozenset({
    "cache_creation", "cache_creation_input_tokens", "cache_read_input_tokens",
    "inference_geo", "input_tokens", "iterations", "output_tokens",
    # output_tokens_details breaks output down (e.g. thinking_tokens); those are
    # already inside output_tokens and are never billed separately.
    "output_tokens_details",
    "server_tool_use", "service_tier", "speed",
})

# Keys of the nested ``usage.cache_creation`` breakdown (5m / 1h ephemeral caches).
KNOWN_CACHE_CREATION_KEYS = frozenset({
    "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
})

# Claude Code versions present when the allowlists above were last refreshed. The
# doctor reports any version not in this set as "unseen" — a prompt to re-verify the
# envelope against that release. Update alongside the allowlists.
KNOWN_VERSIONS = frozenset({
    "2.1.92", "2.1.111", "2.1.119", "2.1.121", "2.1.126", "2.1.128",
    "2.1.138", "2.1.142", "2.1.146", "2.1.148", "2.1.149", "2.1.150",
    "2.1.156", "2.1.158", "2.1.159", "2.1.160", "2.1.161", "2.1.165",
    "2.1.170", "2.1.177", "2.1.179", "2.1.181", "2.1.183", "2.1.185",
    "2.1.187", "2.1.196", "2.1.197", "2.1.198", "2.1.200", "2.1.202",
    "2.1.205", "2.1.206", "2.1.207", "2.1.208", "2.1.209", "2.1.215",
    "2.1.217", "2.1.219", "2.1.220", "2.1.221", "2.1.222", "2.1.223",
    "2.1.224",
})


# ── Codex allowlists ─────────────────────────────────────────────────────────────
# Captured from the user's real ~/.codex/sessions logs. Codex has its own
# envelope, entirely distinct from Claude's: every line is
# ``{timestamp, type, payload}`` and token accounting lives only inside an
# ``event_msg`` whose ``payload.type == "token_count"``.

KNOWN_CODEX_ENVELOPE_KEYS = frozenset({"payload", "timestamp", "type"})

# Envelope ``type`` values. token_count (the money record) rides inside event_msg.
# world_state (env/AGENTS.md snapshot) and inter_agent_communication_metadata
# (multi-agent trigger flag) are 0.14x additions — no token data.
KNOWN_CODEX_TYPES = frozenset({
    "compacted", "event_msg", "inter_agent_communication_metadata",
    "response_item", "session_meta", "turn_context", "world_state",
})

# ``payload.type`` values across event_msg / response_item. A genuinely new one is
# drift — it may carry data (tokens, a new tool) we'd silently miss.
KNOWN_CODEX_PAYLOAD_TYPES = frozenset({
    "agent_message", "agent_reasoning", "context_compacted", "custom_tool_call",
    "custom_tool_call_output", "exec_command_end", "function_call",
    "function_call_output", "image_generation_call", "image_generation_end",
    "item_completed", "mcp_tool_call_end", "message", "patch_apply_end",
    "reasoning", "sub_agent_activity", "task_complete", "task_started",
    "thread_name_updated", "thread_rolled_back", "thread_settings_applied",
    "token_count", "tool_search_call", "tool_search_output",
    "turn_aborted", "user_message", "view_image_tool_call", "web_search_call",
    "web_search_end",
})

# Keys of ``token_count.info``. ``*_token_usage`` are the usage sub-objects.
KNOWN_CODEX_INFO_KEYS = frozenset({
    "last_token_usage", "total_token_usage", "model_context_window",
})

# Keys of each ``*_token_usage`` object — the highest-value Codex canary: a new
# token bucket here is usage we'd silently stop counting.
KNOWN_CODEX_USAGE_KEYS = frozenset({
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
})

# Usage keys we recognise but have only ever observed as **zero**, so we do not
# yet map them into a DB column.
#
# ``cache_write_input_tokens`` arrived with Codex CLI 0.146.0 in both usage slots
# and has never been populated (2,438 occurrences, every one 0 — verified against
# the real logs, August 2026). We deliberately do NOT wire it into
# ``cache_creation_tokens`` yet: Codex's ``input_tokens`` *includes*
# ``cached_input_tokens`` (hence the ``inp - cached`` in sources/codex.py), and
# whether it also includes the cache-write tokens is unknowable while every value
# is 0. Guessing wrong either double-counts input or under-counts it.
#
# Plain-allowlisting the key would silence the alarm along with the noise, so the
# doctor keeps a *value*-level canary instead: the first nonzero value is drift.
# When it fires, confirm the semantics and — if they mirror ``cached_input_tokens``
# — the expected fix is the symmetric ``fresh_input = inp - cached - cache_write``
# plus mapping cache_write into ``cache_creation_tokens`` in sources/codex.py.
KNOWN_CODEX_ZERO_ONLY_USAGE_KEYS = frozenset({"cache_write_input_tokens"})

# Iteration types inside ``usage.iterations[]`` that the scanner knows how to
# account for: 'message' (the ordinary inference, already counted in the
# envelope's top-level tokens) and 'advisor_message' (a separate inference on
# the record's ``advisorModel``, NOT in the top-level tokens — the scanner emits
# it as its own turn). A new type here is billable spend nobody is recording.
KNOWN_ITERATION_TYPES = frozenset({"message", "advisor_message"})


def _is_nonzero(value):
    """True if `value` is a number (or numeric string) other than zero.

    Mirrors the coercion in sources/codex.py's ``_int`` so a stringified "512"
    still trips the canary; anything uncoercible is not treated as nonzero.
    """
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


def inspect_record(record):
    """Return the unknown surface of a single parsed JSONL record.

    Reports keys / type / version that fall outside the allowlists above. An
    all-empty result (no unknown_* lists, unknown_type is None) means the record
    conforms to the known schema surface. ``version`` is echoed back for the
    version tripwire (the caller decides whether it is in KNOWN_VERSIONS).
    """
    if not isinstance(record, dict):
        return {
            "type": None, "version": None,
            "unknown_record_keys": [], "unknown_message_keys": [],
            "unknown_usage_keys": [], "unknown_cache_creation_keys": [],
            "unknown_iteration_types": [],
            "unknown_type": "<not-an-object>", "shape_drift": [],
        }

    rtype = record.get("type")
    result = {
        "type": rtype,
        "version": record.get("version"),
        "unknown_record_keys": sorted(k for k in record if k not in KNOWN_RECORD_KEYS),
        "unknown_message_keys": [],
        "unknown_usage_keys": [],
        "unknown_cache_creation_keys": [],
        "unknown_iteration_types": [],
        "unknown_type": rtype if (rtype is not None and rtype not in KNOWN_RECORD_TYPES) else None,
        # Nested fields whose *type* drifted (e.g. message is a list, not a dict).
        # The scanner assumes these are dicts, so a wrong type is real drift — not
        # something to skip past silently. Recorded as "<field>:<python-type>".
        "shape_drift": [],
    }

    msg = record.get("message")
    if msg is not None and not isinstance(msg, dict):
        result["shape_drift"].append(f"message:{type(msg).__name__}")
    elif isinstance(msg, dict):
        result["unknown_message_keys"] = sorted(
            k for k in msg if k not in KNOWN_MESSAGE_KEYS
        )
        usage = msg.get("usage")
        if usage is not None and not isinstance(usage, dict):
            result["shape_drift"].append(f"usage:{type(usage).__name__}")
        elif isinstance(usage, dict):
            result["unknown_usage_keys"] = sorted(
                k for k in usage if k not in KNOWN_USAGE_KEYS
            )
            cc = usage.get("cache_creation")
            if cc is not None and not isinstance(cc, dict):
                result["shape_drift"].append(f"cache_creation:{type(cc).__name__}")
            elif isinstance(cc, dict):
                result["unknown_cache_creation_keys"] = sorted(
                    k for k in cc if k not in KNOWN_CACHE_CREATION_KEYS
                )
            # `usage.iterations[]` splits one assistant message into the separate
            # inferences that produced it. This is a *value-level* canary because
            # the `iterations` key itself was allowlisted while its contents
            # carried unrecorded money: the envelope's top-level input/output
            # tokens count only `message` iterations, so `advisor_message` spend
            # was invisible until the scanner started emitting it as its own turn.
            # Any further type is another billable class we are not recording yet.
            iters = usage.get("iterations")
            if iters is not None and not isinstance(iters, list):
                result["shape_drift"].append(f"iterations:{type(iters).__name__}")
            elif isinstance(iters, list):
                result["unknown_iteration_types"] = sorted({
                    str(it.get("type")) for it in iters
                    if isinstance(it, dict) and it.get("type") not in KNOWN_ITERATION_TYPES
                })
    return result


def record_is_known(record):
    """True if a record introduces no unknown keys, type, or shape drift."""
    r = inspect_record(record)
    return not (
        r["unknown_record_keys"] or r["unknown_message_keys"]
        or r["unknown_usage_keys"] or r["unknown_cache_creation_keys"]
        or r["unknown_iteration_types"]
        or r["unknown_type"] or r["shape_drift"]
    )


def _iter_jsonl(projects_dirs):
    """Yield .jsonl file paths under each existing projects dir (sorted)."""
    files = []
    for d in projects_dirs:
        d = Path(d)
        if d.exists():
            files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    files.sort()
    return files


def doctor_scan(projects_dirs, file_limit=None):
    """Scan real transcript logs and report schema drift, without touching the DB.

    Aggregates, across every record in every .jsonl file under ``projects_dirs``:
    unknown keys (per level, with occurrence counts), unknown record types, and the
    set of Claude Code versions present (split into known vs. unseen). Read-only:
    the doctor never writes the database or alters parsing — it just *looks*.

    ``file_limit`` caps the number of files scanned (newest first by mtime is the
    caller's job; here we keep the deterministic sorted order). Returns a dict
    consumable by ``format_doctor_report``.
    """
    from collections import Counter
    import os

    files = _iter_jsonl(projects_dirs)
    if file_limit is not None:
        files = files[:file_limit]

    unknown_record_keys = Counter()
    unknown_message_keys = Counter()
    unknown_usage_keys = Counter()
    unknown_cache_creation_keys = Counter()
    unknown_iteration_types = Counter()
    unknown_types = Counter()
    shape_drift = Counter()
    versions = Counter()
    records_scanned = 0
    files_scanned = 0

    for fp in files:
        files_scanned += 1
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Non-dict JSON lines are not skipped: inspect_record reports
                    # them as the "<not-an-object>" type so the doctor still flags
                    # a transcript that isn't a stream of objects.
                    records_scanned += 1
                    info = inspect_record(record)
                    if info["version"]:
                        versions[info["version"]] += 1
                    if info["unknown_type"]:
                        unknown_types[info["unknown_type"]] += 1
                    for k in info["unknown_record_keys"]:
                        unknown_record_keys[k] += 1
                    for k in info["unknown_message_keys"]:
                        unknown_message_keys[k] += 1
                    for k in info["unknown_usage_keys"]:
                        unknown_usage_keys[k] += 1
                    for k in info["unknown_cache_creation_keys"]:
                        unknown_cache_creation_keys[k] += 1
                    for k in info["unknown_iteration_types"]:
                        unknown_iteration_types[k] += 1
                    for k in info["shape_drift"]:
                        shape_drift[k] += 1
        except OSError:
            continue

    seen_versions = set(versions)
    return {
        "files_scanned": files_scanned,
        "records_scanned": records_scanned,
        "versions": dict(versions),
        "known_versions": sorted(seen_versions & KNOWN_VERSIONS),
        "unseen_versions": sorted(seen_versions - KNOWN_VERSIONS),
        "unknown_types": dict(unknown_types),
        "unknown_record_keys": dict(unknown_record_keys),
        "unknown_message_keys": dict(unknown_message_keys),
        "unknown_usage_keys": dict(unknown_usage_keys),
        "unknown_cache_creation_keys": dict(unknown_cache_creation_keys),
        "unknown_iteration_types": dict(unknown_iteration_types),
        "shape_drift": dict(shape_drift),
    }


def report_is_clean(report):
    """True if the doctor found no drift (no unknown keys/types, no unseen versions)."""
    return not (
        report["unseen_versions"] or report["unknown_types"]
        or report["unknown_record_keys"] or report["unknown_message_keys"]
        or report["unknown_usage_keys"] or report["unknown_cache_creation_keys"]
        or report["unknown_iteration_types"]
        or report["shape_drift"]
    )


def format_doctor_report(report):
    """Render a doctor_scan() report as a human-readable multi-line string."""
    lines = []
    lines.append("=" * 60)
    lines.append("  scan --doctor : schema-drift report")
    lines.append("=" * 60)
    lines.append(f"  Files scanned:   {report['files_scanned']}")
    lines.append(f"  Records scanned: {report['records_scanned']}")

    def section(title, counts):
        lines.append("-" * 60)
        if counts:
            lines.append(f"  {title}:")
            for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {key}  (x{n})")
        else:
            lines.append(f"  {title}: none")

    # Versions
    lines.append("-" * 60)
    known = report["known_versions"]
    unseen = report["unseen_versions"]
    lines.append(f"  Versions seen (known):  {', '.join(known) if known else 'none'}")
    if unseen:
        lines.append(f"  Versions UNSEEN:        {', '.join(unseen)}")
        lines.append("    ^ new Claude Code release(s): re-verify the envelope and")
        lines.append("      refresh the allowlists / KNOWN_VERSIONS in schema.py.")
    else:
        lines.append("  Versions unseen:        none")

    section("Unknown record types", report["unknown_types"])
    section("Unknown top-level keys", report["unknown_record_keys"])
    section("Unknown message keys", report["unknown_message_keys"])
    section("Unknown usage keys", report["unknown_usage_keys"])
    section("Unknown cache_creation keys", report["unknown_cache_creation_keys"])
    section("Unknown usage.iterations types", report["unknown_iteration_types"])
    section("Shape drift (wrong nested type)", report["shape_drift"])

    lines.append("=" * 60)
    if report_is_clean(report):
        lines.append("  RESULT: clean - no schema drift detected.")
    else:
        lines.append("  RESULT: DRIFT DETECTED — see flagged items above.")
    lines.append("=" * 60)
    return "\n".join(lines)


# ── Codex drift detection ────────────────────────────────────────────────────────

def inspect_codex_record(record):
    """Return the unknown surface of a single Codex rollout record.

    Reports envelope keys / type / payload-type / token-usage keys that fall outside
    the Codex allowlists. ``version`` echoes ``session_meta.cli_version`` (only that
    record carries it). An all-empty result means the record conforms.
    """
    empty = {
        "type": None, "payload_type": None, "version": None,
        "unknown_envelope_keys": [], "unknown_type": None,
        "unknown_payload_type": None, "unknown_usage_keys": [],
        "unknown_info_keys": [], "shape_drift": [],
        "nonzero_zero_only_usage_keys": [],
    }
    if not isinstance(record, dict):
        return {**empty, "unknown_type": "<not-an-object>"}

    etype = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    ptype = payload.get("type")

    result = {
        **empty,
        "type": etype,
        "payload_type": ptype,
        "unknown_envelope_keys": sorted(k for k in record if k not in KNOWN_CODEX_ENVELOPE_KEYS),
        "unknown_type": etype if (etype is not None and etype not in KNOWN_CODEX_TYPES) else None,
        "unknown_payload_type": ptype if (ptype is not None and ptype not in KNOWN_CODEX_PAYLOAD_TYPES) else None,
    }

    if etype == "session_meta":
        result["version"] = payload.get("cli_version")

    if ptype == "token_count":
        info = payload.get("info")
        # info is legitimately null on some records (older CLI) — not drift.
        if info is not None and not isinstance(info, dict):
            result["shape_drift"].append(f"info:{type(info).__name__}")
        elif isinstance(info, dict):
            result["unknown_info_keys"] = sorted(
                k for k in info if k not in KNOWN_CODEX_INFO_KEYS
            )
            usage_unknown = set()
            zero_only_nonzero = set()
            for uk in ("last_token_usage", "total_token_usage"):
                u = info.get(uk)
                if u is not None and not isinstance(u, dict):
                    result["shape_drift"].append(f"{uk}:{type(u).__name__}")
                elif isinstance(u, dict):
                    usage_unknown.update(k for k in u if k not in KNOWN_CODEX_USAGE_KEYS)
                    # Value-level canary: a bucket we allowlisted only because it
                    # has always been 0 has started carrying tokens.
                    zero_only_nonzero.update(
                        k for k in KNOWN_CODEX_ZERO_ONLY_USAGE_KEYS if _is_nonzero(u.get(k))
                    )
            result["unknown_usage_keys"] = sorted(usage_unknown)
            result["nonzero_zero_only_usage_keys"] = sorted(zero_only_nonzero)

    return result


def codex_record_is_known(record):
    """True if a Codex record introduces no unknown surface or shape drift."""
    r = inspect_codex_record(record)
    return not (
        r["unknown_envelope_keys"] or r["unknown_type"] or r["unknown_payload_type"]
        or r["unknown_usage_keys"] or r["unknown_info_keys"] or r["shape_drift"]
    )


def _iter_codex_jsonl(codex_paths):
    """Yield Codex rollout file paths under sessions/ + archived_sessions/ (sorted).

    Scoped to those two subtrees so unrelated ~/.codex cruft is never inspected.
    """
    files = []
    for base in codex_paths:
        base = Path(base)
        for sub in ("sessions", "archived_sessions"):
            d = base / sub
            if d.exists():
                files.extend(glob.glob(str(d / "**" / "rollout-*.jsonl"), recursive=True))
    files.sort()
    return files


def codex_doctor_scan(codex_paths, file_limit=None):
    """Scan real Codex logs and report envelope drift, without touching the DB.

    Codex CLI versions churn fast (frequent alphas), so versions are reported
    *informationally* and never gate the result — the hard gate is structural
    surface drift (unknown envelope keys / types / payload types / token-usage
    keys), which is what would silently break token capture.
    """
    from collections import Counter

    files = _iter_codex_jsonl(codex_paths)
    if file_limit is not None:
        files = files[:file_limit]

    unknown_envelope_keys = Counter()
    unknown_types = Counter()
    unknown_payload_types = Counter()
    unknown_usage_keys = Counter()
    unknown_info_keys = Counter()
    nonzero_zero_only_usage_keys = Counter()
    shape_drift = Counter()
    versions = Counter()
    records_scanned = 0
    files_scanned = 0

    for fp in files:
        files_scanned += 1
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records_scanned += 1
                    info = inspect_codex_record(record)
                    if info["version"]:
                        versions[info["version"]] += 1
                    if info["unknown_type"]:
                        unknown_types[info["unknown_type"]] += 1
                    if info["unknown_payload_type"]:
                        unknown_payload_types[info["unknown_payload_type"]] += 1
                    for k in info["unknown_envelope_keys"]:
                        unknown_envelope_keys[k] += 1
                    for k in info["unknown_usage_keys"]:
                        unknown_usage_keys[k] += 1
                    for k in info["unknown_info_keys"]:
                        unknown_info_keys[k] += 1
                    for k in info["nonzero_zero_only_usage_keys"]:
                        nonzero_zero_only_usage_keys[k] += 1
                    for k in info["shape_drift"]:
                        shape_drift[k] += 1
        except OSError:
            continue

    return {
        "files_scanned": files_scanned,
        "records_scanned": records_scanned,
        "versions": dict(versions),
        "unknown_types": dict(unknown_types),
        "unknown_payload_types": dict(unknown_payload_types),
        "unknown_envelope_keys": dict(unknown_envelope_keys),
        "unknown_usage_keys": dict(unknown_usage_keys),
        "unknown_info_keys": dict(unknown_info_keys),
        "nonzero_zero_only_usage_keys": dict(nonzero_zero_only_usage_keys),
        "shape_drift": dict(shape_drift),
    }


def codex_report_is_clean(report):
    """True if the Codex doctor found no STRUCTURAL drift (versions don't gate).

    A zero-only usage bucket that has started carrying tokens counts as drift:
    it is money we are not yet recording (see KNOWN_CODEX_ZERO_ONLY_USAGE_KEYS).
    ``.get`` keeps older report dicts (pre-canary) loadable.
    """
    return not (
        report["unknown_types"] or report["unknown_payload_types"]
        or report["unknown_envelope_keys"] or report["unknown_usage_keys"]
        or report["unknown_info_keys"] or report["shape_drift"]
        or report.get("nonzero_zero_only_usage_keys")
    )


def format_codex_doctor_report(report):
    """Render a codex_doctor_scan() report as a human-readable string."""
    lines = []
    lines.append("=" * 60)
    lines.append("  scan --doctor : Codex schema-drift report")
    lines.append("=" * 60)
    lines.append(f"  Files scanned:   {report['files_scanned']}")
    lines.append(f"  Records scanned: {report['records_scanned']}")

    def section(title, counts):
        lines.append("-" * 60)
        if counts:
            lines.append(f"  {title}:")
            for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {key}  (x{n})")
        else:
            lines.append(f"  {title}: none")

    lines.append("-" * 60)
    vers = sorted(report["versions"])
    lines.append(f"  Codex CLI versions seen ({len(vers)}, informational):")
    lines.append(f"    {', '.join(vers) if vers else 'none'}")

    section("Unknown envelope types", report["unknown_types"])
    section("Unknown payload types", report["unknown_payload_types"])
    section("Unknown envelope keys", report["unknown_envelope_keys"])
    section("Unknown token-usage keys", report["unknown_usage_keys"])
    section("Unknown token_count.info keys", report["unknown_info_keys"])
    section("Zero-only token buckets now NONZERO (unrecorded tokens!)",
            report.get("nonzero_zero_only_usage_keys") or {})
    section("Shape drift (wrong nested type)", report["shape_drift"])

    lines.append("=" * 60)
    if codex_report_is_clean(report):
        lines.append("  RESULT: clean - no Codex schema drift detected.")
    else:
        lines.append("  RESULT: DRIFT DETECTED — see flagged items above.")
    lines.append("=" * 60)
    return "\n".join(lines)
