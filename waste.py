"""Session cost-waste attribution built from the read-only ``turns`` table.

``resident_output`` and ``repeated_reads`` charge only the next turn's
cache-write, not cache-reads carried by later turns. Their dollars therefore
under-report absolute cost and are useful for ranking, not totals.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pricing
import provenance
from scanner import ADVISOR_ID_PREFIX, AGENT_SUMMARY_ID_PREFIX


CAUSES = ("resident_output", "repeated_reads", "subagent_preamble", "idle_gap", "uncached_input",
          "cache_miss_rebuild", "deferred_tool_load")
# Evidence tiers (plan decision 4). Nothing here is measured before/after, so no cause may
# claim "demonstrated saving" until v1.1.
TIERS = {"resident_output": "attributed cause", "repeated_reads": "attributed cause",
         "subagent_preamble": "attributed cause", "idle_gap": "attributed cause",
         "uncached_input": "observation", "cache_miss_rebuild": "attributed cause",
         "deferred_tool_load": "inferred"}
# One pricing basis for findings and totals: pricing.calc_cost at API list rate.
BASIS = {"pricing": "API list rate",
         "label": "attributed, lower bound, at API list rate; not savings"}
# Sources a detector is validated for. Anything else is shown "not measurable", never silent.
_MEASURED = {"claude"}
_WHY = {"codex": "Codex reports cached input but no cache-write tokens, tool-call file paths or "
                 "sidechain turns, so this detector has no signal"}
# Causes inferred (not observed) for a source: Codex has no cache-write, so resident_output is read
# from per-request input growth (input + cache_read) after a tool call.
_INFERRED = {"codex": {"resident_output": "inferred from per-request input growth after a tool call "
                                          "(Codex reports no cache-write tokens); not observed"}}
_WHY_DEFAULT = "no detector is validated for this source"
def _synthetic(row):
    """Scanner-minted estimate rows. An advisor row under a parent with no message.id is
    stored with an empty id (scanner keeps it un-deduped), so it is also known by its tool."""
    mid = row["message_id"] or ""
    return (mid.startswith((ADVISOR_ID_PREFIX, AGENT_SUMMARY_ID_PREFIX))
            or (not mid and row["tool_name"] == "advisor" and not row["tool_calls"]))


def measurement(db_path):
    """Per cause x source: measured, or not measurable with the reason. Sources are those in the DB."""
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        sources = sorted({r[0] or "claude" for r in conn.execute("SELECT DISTINCT agent FROM turns")})
    finally:
        conn.close()
    cell = lambda src, cause: ({"status": "measured", "reason": ""} if src in _MEASURED else
                               {"status": "inferred", "reason": _INFERRED[src][cause]} if cause in _INFERRED.get(src, ()) else
                               {"status": "not measurable", "reason": _WHY.get(src, _WHY_DEFAULT)})
    return {"sources": sources, "causes": {c: {s: cell(s, c) for s in sources} for c in CAUSES}}


def _calls(row):
    try:
        calls = json.loads(row["tool_calls"] or "[]")
    except (TypeError, ValueError):
        return []
    return calls if isinstance(calls, list) else []


def _tool_info(row, seen_paths):
    calls = _calls(row)
    repeated = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get("name") != "Read":
            continue
        path = (call.get("input") or {}).get("file_path")
        if path in seen_paths:
            repeated.append(path)
        if path:
            seen_paths.add(path)
    return bool(calls or row["tool_name"]), repeated


def _cost(row, inp=0, creation=0):
    return pricing.calc_cost(row["model"], inp, 0, 0, creation,
                             row["timestamp"], row["cache_creation_1h_tokens"] if creation else 0) * row["mult"]


def _claim(claims, cause, turn, tokens, detail, tool, inp=False):
    usd = _cost(turn, inp=tokens) if inp else _cost(turn, creation=tokens)
    claims[cause][0] += usd
    claims[cause][1].append(detail)
    claims[cause][2].append({"at": turn["timestamp"], "tokens": tokens, "usd": usd, "tool": tool,
                              "message_id": turn["message_id"], "agent_id": turn["agent_id"]})


def _codex_resident(claims, turn, state):
    """Codex resident_output: context growth (input + cache_read) over the previous request, when
    that request called a tool. Charged at the fresh-input rate, capped at the turn's fresh input."""
    ctx = (turn["input_tokens"] or 0) + (turn["cache_read_tokens"] or 0)
    grown = min(ctx - state["ctx"], turn["input_tokens"] or 0) if state["tool"] else 0
    if grown > 0:
        _claim(claims, "resident_output", turn, grown, (state["tool_name"] or "tool", grown),
               state["tool_name"], inp=True)
    state.update(ctx=ctx, tool=bool(_calls(turn) or turn["tool_name"]) and not turn["is_compact_summary"],
                 tool_name=turn["tool_name"])


def _gap(a, b):
    try:
        return datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _ttl(turn):
    """Cache TTL in seconds for the tier this turn wrote at (static/modules/pricing.js rewriteKinds mirrors it)."""
    return 3600 if (turn["cache_creation_1h_tokens"] or 0) > 0 else 300


def _rebuild_reason(turn, prev):
    """cache_miss_rebuild (Claude main chain only): a turn that rewrote most of the prefix its
    predecessor had cached. Returns None (not a rebuild), ("expected", why) or ("unexpected", likely cause).
    Expected: gap >= TTL, partial 5m-tier expiry, compaction, model switch, fast-mode switch. /clear starts a new session id, so it shows as an
    opening prefix, never as a rebuild here."""
    creation, read = turn["cache_creation_tokens"] or 0, turn["cache_read_tokens"] or 0
    prefix = prev["prefix"]
    if not creation:
        return None
    gap = _gap(prev["timestamp"], turn["timestamp"])
    # Partial expiry: the prefix was cached at the 1h tier, so the 1h part survives (read > 0) while the
    # 5m part expired; the rewrite is that 5m part. Checked before the majority cutoff, since the surviving
    # 1h part is usually most of the prefix. A full miss (read == 0) is still a changed prefix, and
    # read >= prefix means nothing expired (plain growth).
    # ponytail: 'a 1h write is still in the prefix' is tracked as a flag, not per-token tier sizes.
    if 0 < read < prefix and gap is not None and gap.total_seconds() >= 300 and prev.get("h1", prev["ttl"] > 300):
        return "expected", f"idle {gap.total_seconds() / 60:.0f} min, past the 5 min TTL of the 5m-tier part (1h-tier part still cached)"
    if read * 2 >= prefix:
        return None
    if gap is not None and gap.total_seconds() >= prev["ttl"]:  # the cached prefix's tier, not the rewrite's
        return "expected", f"idle {gap.total_seconds() / 60:.0f} min, past the {prev['ttl'] // 60} min cache TTL"
    if turn["is_compact_summary"] or prev["compact"]:
        return "expected", "context compaction"
    if turn["model"] != prev["model"]:
        return "expected", f"model switch ({prev['model']} to {turn['model']})"
    # Requests at different speeds share no cached prefix, so turning fast mode on or off rewrites it.
    was_fast, is_fast = prev.get("speed") == "fast", "speed" in turn.keys() and turn["speed"] == "fast"
    if was_fast != is_fast:
        return "expected", f"speed switch ({'fast' if was_fast else 'standard'} to {'fast' if is_fast else 'standard'})"
    return "unexpected", (f"read {read:,} of {prefix:,} cached tokens inside the TTL with no compaction, model or speed switch: "
                          "the prompt prefix changed (system prompt, tool set or settings edit)")


def _mcp_parts(name):
    m = re.match(r"mcp__(.+?)__(.+)$", name)
    return m.groups() if m else (None, name)


def _same_install(a, b):
    """Two MCP prefixes that are one server installed twice (plugin_<plugin>_<server> vs <server>)."""
    return a != b and (a.endswith("_" + b) or b.endswith("_" + a))


def _overlaps(events):
    """Session-wide: names that duplicate a tool an earlier-loaded server already provided (same
    suffix under two MCP prefixes), plus the (first, later) server pairs.
    ponytail: one shared suffix is enough, so a generic name (search, list) can false-positive; the
    10% share floor keeps those from surfacing. Tighten to a Jaccard threshold if it misfires."""
    first = {}  # suffix -> first server that loaded it
    dup, pairs = set(), set()
    for names in events:
        for name in names:
            server, suffix = _mcp_parts(name)
            if server is None:
                continue
            if suffix not in first:
                first[suffix] = server
            elif first[suffix] != server:
                dup.add(name)
                pairs.add((first[suffix], server))
    return dup, pairs


def _deferred_tool_load(turns, loads, claims):
    """ToolSearch schemas stay resident and are re-read every later turn. Per lane (main
    thread or one subagent), a load is waste for the names never called afterwards, reloaded while
    still in context (no compaction between), or duplicating another MCP server's tool. Priced as the
    next turn's cache-write above the lane's median write (inferred proxy) x the lane's later turns at
    the cache-read rate, for the wasted share of that load. Those later turns' reads are claimed by no
    other cause; turns idle_gap / cache_miss_rebuild claimed are skipped; one claim per next-turn."""
    lanes = {}
    for t in turns:
        if _synthetic(t) or (t["is_sidechain"] and not t["agent_id"]):
            continue
        lanes.setdefault(t["agent_id"] if t["is_sidechain"] else None, []).append(t)
    if not any(t["message_id"] in loads for lt in lanes.values() for t in lt):
        return
    pairs = set()
    claimed = {e["message_id"] for c in ("idle_gap", "cache_miss_rebuild") for e in claims[c][2]}
    used, loaders = set(), {}
    for lane, lt in lanes.items():
        last_call, compact_at = {}, [-1]
        for j, t in enumerate(lt):
            if t["is_compact_summary"]:
                compact_at.append(j)
            for call in _calls(t):
                if isinstance(call, dict) and call.get("name"):
                    last_call[call["name"]] = j
        writes = sorted(t["cache_creation_tokens"] for t in lt if t["cache_creation_tokens"])
        median = writes[len(writes) // 2] if writes else 0
        # Overlap is judged inside one lane: two subagents each using a different server is not a duplicate.
        dup_names, lane_pairs = _overlaps([loads[t["message_id"]] for t in lt if t["message_id"] in loads])
        pairs |= lane_pairs
        seen = {}  # name -> index of its latest load in this lane
        for i, t in enumerate(lt):
            names = loads.get(t["message_id"])
            if not names:
                continue
            unused = [n for n in names if last_call.get(n, -1) <= i]
            reloaded = [n for n in names if n in seen and not any(seen[n] < k <= i for k in compact_at)]
            overlap = [n for n in names if n in dup_names]
            for n in names:
                seen[n] = i
                if lane is not None:
                    loaders.setdefault(n, set()).add(lane)
            waste = set(unused) | set(reloaded) | set(overlap)
            if not waste or i + 1 >= len(lt):
                continue
            nxt = lt[i + 1]
            # the schemas leave context at the next compaction (even one on the very next turn), so the
            # charge stops there; each later turn counts only if it read the cache and no other cause claimed it
            stop = min((k for k in compact_at if k > i), default=len(lt))
            remaining = sum(1 for u in lt[i + 2:stop]
                            if (u["cache_read_tokens"] or 0) and u["message_id"] not in claimed)
            delta = max(0, (nxt["cache_creation_tokens"] or 0) - median)
            if not delta or not remaining or nxt["id"] in used or nxt["message_id"] in claimed:
                continue
            used.add(nxt["id"])
            tokens = int(delta * len(waste) / len(names) * remaining)
            usd = pricing.calc_cost(nxt["model"], 0, 0, tokens, 0, nxt["timestamp"]) * nxt["mult"]
            claims["deferred_tool_load"][0] += usd
            claims["deferred_tool_load"][1].append(
                {"loaded": len(names), "unused": unused, "reloaded": reloaded, "overlap": overlap})
            claims["deferred_tool_load"][2].append(
                {"at": nxt["timestamp"], "tokens": tokens, "usd": usd, "tool": "ToolSearch",
                 "message_id": t["message_id"], "agent_id": t["agent_id"]})
    if claims["deferred_tool_load"][1]:
        # Observation, not extra waste: each subagent lane already paid for its own copy above.
        claims["deferred_tool_load"][1].append(
            {"pairs": sorted(pairs), "shared": sorted(n for n, ls in loaders.items() if len(ls) > 1)})


MIN_SHARE = 0.10  # Claude Code /usage flags behaviors at >=10% of spend; same default here


def report(db_path, session_id=None, min_share=MIN_SHARE, rebuilds=None, min_usd=.01):
    """Return thresholded, disjoint cost findings for one session or a database.

    A cause is dropped when it is under min_share of its session's total $.
    If ``rebuilds`` is a list, expected cache rebuilds (reported, not waste) are appended to it."""
    db_path = Path(db_path).resolve().as_posix()
    # quote: an unencoded '#' or '?' in the path would silently open a different database
    conn = sqlite3.connect(f"file:{quote(str(db_path), safe='/:')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = """SELECT id, session_id, timestamp, model, input_tokens, output_tokens,
                         cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens,
                         tool_name, tool_calls, is_sidechain, is_compact_summary, agent,
                         {message_id} AS message_id, {agent_id} AS agent_id, {speed} AS speed, {mult} AS mult
                  FROM turns"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        sql = sql.format(mult=pricing.price_mult_sql(cols),
                         **{c: c if c in cols else "NULL" for c in ("message_id", "agent_id", "speed")})
        rows = conn.execute(sql + (" WHERE session_id=?" if session_id else "") +
                            " ORDER BY session_id, timestamp, id", (session_id,) if session_id else ()).fetchall()
        loads = {}  # message_id -> tool names a ToolSearch result loaded
        if "loaded_tools" in {r[1] for r in conn.execute("PRAGMA table_info(tool_results)")}:
            for mid, names in conn.execute("SELECT message_id, loaded_tools FROM tool_results "
                                           "WHERE loaded_tools IS NOT NULL AND message_id IS NOT NULL"):
                try:
                    loads[mid] = list(dict.fromkeys(loads.get(mid, []) + json.loads(names)))
                except (TypeError, ValueError):
                    pass
    finally:
        conn.close()

    sessions = {}
    for row in rows:
        if (row["agent"] or "claude") not in _MEASURED and "resident_output" not in _INFERRED.get(row["agent"], ()):
            continue  # unmeasurable sources attribute nothing; measurement() says so
        sessions.setdefault(row["session_id"], []).append(row)
    findings = []
    for sid, turns in sessions.items():
        claims = {cause: [0.0, [], []] for cause in CAUSES}
        seen_paths, previous_main, previous_turn = set(), None, None
        lanes = {}  # per-subagent state: each agent's cache prefix is its own
        cclaims = {cause: [0.0, [], []] for cause in CAUSES}  # inferred-source claims, kept apart
        cx = {"ctx": 0, "tool": False, "tool_name": None}
        for turn in turns:
            if turn["agent"] in _INFERRED:  # per turn: a resumed session id can mix sources
                _codex_resident(cclaims, turn, cx)
                continue
            creation = turn["cache_creation_tokens"] or 0
            if _synthetic(turn):
                # Scanner-minted estimates (advisor iterations, AgentSummary forks): their
                # cost stays in the session total, but they carry no cache prefix or tool
                # output of their own, so they claim nothing and must not move lane state.
                previous_turn = turn
                continue
            if turn["is_sidechain"] and not turn["agent_id"]:
                # Rows with no (or an empty) agent_id: interleaved agents cannot be told apart, so
                # only a cold write (no cache_read) is claimed, as a preamble; growth stays
                # unattributed rather than charged to another agent's tool.
                if creation and not (turn["cache_read_tokens"] or 0):
                    _claim(claims, "subagent_preamble", turn, creation, creation, "Agent")
                previous_turn = turn
                continue
            if turn["is_sidechain"]:
                # Only an agent's first cache write is its preamble; later writes are its own
                # context growth, attributed like main-chain growth.
                lane = lanes.setdefault(turn["agent_id"], {"seen": set(), "prev": None, "preamble": False})
                compact = turn["is_compact_summary"]  # same guard as the main chain below
                has_tool, repeated = _tool_info(turn, lane["seen"]) if not compact else (False, [])
                prev = lane["prev"]
                if creation and not lane["preamble"]:
                    lane["preamble"] = True
                    _claim(claims, "subagent_preamble", turn, creation, creation, "Agent")
                elif creation and not compact and prev and prev["repeated"]:
                    _claim(claims, "repeated_reads", turn, creation, (prev["repeated"], creation), "Read")
                elif creation and not compact and prev and prev["has_tool"]:
                    _claim(claims, "resident_output", turn, creation,
                           (prev["tool_name"] or "tool", creation), prev["tool_name"])
                lane["prev"] = {"has_tool": has_tool, "repeated": repeated, "tool_name": turn["tool_name"]}
                previous_turn = turn
                continue
            compact = turn["is_compact_summary"]
            has_tool, repeated = _tool_info(turn, seen_paths) if not compact else (False, [])
            gap = _gap(previous_turn["timestamp"], turn["timestamp"]) if previous_turn else None
            if creation and previous_main:
                if gap and gap.total_seconds() >= previous_main["ttl"]:  # TTL of the tier the prefix was cached at
                    # same tokens as cache reads = what a warm (or compacted-and-warm) cache would have billed
                    warm = pricing.calc_cost(turn["model"], 0, 0, creation, 0, turn["timestamp"]) * turn["mult"]
                    _claim(claims, "idle_gap", turn, creation, (gap, creation, warm), None)
                elif (why := _rebuild_reason(turn, previous_main)):
                    if why[0] == "unexpected":
                        _claim(claims, "cache_miss_rebuild", turn, creation, why[1], None)
                    elif rebuilds is not None:
                        rebuilds.append({"session_id": sid, "at": turn["timestamp"], "tokens": creation,
                                         "usd": _cost(turn, creation=creation), "reason": why[1]})
                elif not compact and previous_main["repeated"]:
                    _claim(claims, "repeated_reads", turn, creation, (previous_main["repeated"], creation), "Read")
                elif not compact and previous_main["has_tool"]:
                    _claim(claims, "resident_output", turn, creation,
                           (previous_main["tool_name"] or "tool", creation), previous_main["tool_name"])
            if ((turn["agent"] or "claude") == "claude" and not (turn["cache_read_tokens"] or 0)
                    and not creation and (turn["input_tokens"] or 0)):
                _claim(claims, "uncached_input", turn, turn["input_tokens"], turn["input_tokens"], None, inp=True)
            # A 1h write stays in the prefix while it is read back (a full rewrite resets the tier).
            h1 = (turn["cache_creation_1h_tokens"] or 0) > 0 or bool(
                (turn["cache_read_tokens"] or 0) and (previous_main or {}).get("h1"))
            previous_main = {"timestamp": turn["timestamp"], "has_tool": has_tool, "model": turn["model"],
                             "compact": compact, "speed": turn["speed"],
                             "h1": h1,
                             "ttl": 3600 if h1 else 300, "prefix": (turn["cache_read_tokens"] or 0) + creation,
                             "repeated": repeated, "tool_name": turn["tool_name"]}
            previous_turn = turn

        _deferred_tool_load(turns, loads, claims)
        total = sum(pricing.calc_cost(t["model"], t["input_tokens"] or 0, t["output_tokens"] or 0,
                                      t["cache_read_tokens"] or 0, t["cache_creation_tokens"] or 0,
                                      t["timestamp"], t["cache_creation_1h_tokens"] or 0) * t["mult"]
                    for t in turns)
        floor = max(min_usd, total * min_share)
        for cause, (usd, details, evidence), inferred in ([(c, v, False) for c, v in claims.items()] +
                                                          [(c, v, True) for c, v in cclaims.items()]):
            if not evidence or usd < floor:  # min_usd=0 (verify) must still skip causes that never fired
                continue
            if cause == "resident_output":
                ranked = sorted(((sum(n for other, n in details if other == name), name)
                                 for name in {name for name, _ in details}), reverse=True)
                names = ", ".join(f"{name} ({tokens:,})" for tokens, name in ranked[:3])
                if len(ranked) > 3:
                    names += f" (+{len(ranked) - 3} more)"
                detail, fix = (f"{names} caused {sum(n for _, n in details):,} {'input' if inferred else 'cache-write'} tokens of context growth.",
                               # Codex (inferred) has no offset/limit Read or Explore subagent: agent-neutral advice there.
                               # Output styles (caveman etc.) shorten replies, not tool results, so they are not a fix here.
                               ("Keep big tool results out of the conversation: read file ranges and search before you read, "
                                "and cut long command output to its tail or a summary." if inferred else
                                "Keep big tool results out of the main context: Read with offset/limit, Grep before Read, "
                                "pipe long shell output through tail or a summary, and delegate broad reads to an Explore subagent."))
            elif cause == "repeated_reads":
                # basenames only: findings are shown and shared, full paths carry usernames
                files = sorted({re.split(r"[\\/]", str(path))[-1] for paths, _ in details for path in paths})
                paths = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
                detail, fix = (f"Repeated Read calls of {paths} added {sum(n for _, n in details):,} cache-write tokens.",
                               "Read the file once; for a second look, Grep for the part you need or Read just that range with offset/limit.")
            elif cause == "subagent_preamble":
                detail, fix = (f"Sidechain turns wrote {sum(details):,} cache-create tokens for inherited preambles.",
                               "Delegate only reads big enough to pay back the setup; do jobs under ~10 turns in the main session.")
            elif cause == "idle_gap":
                warm = sum(w for _, _, w in details)
                minutes = max((g.total_seconds() / 60 for g, _, _ in details), default=0)
                detail, fix = (f"{len(details)} idle gap{'' if len(details) == 1 else 's'} (longest {minutes:.0f} min) rewrote {sum(n for _, n, _ in details):,} cache tokens "
                               f"for ${usd:.2f}; kept warm that context would have cost ${warm:.2f} (up to ${usd - warm:.2f} saved by compacting before the cache expired).",
                               ("Start a fresh session instead of resuming a stale one; re-onboard from the handoff file."
                                if minutes > 240 else
                                "Resume within the cache TTL (5 min, or 1 h on the long tier), or /clear or /compact before a break."))
            elif cause == "cache_miss_rebuild":
                detail, fix = (f"{len(details)} unexpected cache miss{'' if len(details) == 1 else 'es'} rewrote "
                               f"{sum(e['tokens'] for e in evidence):,} cache tokens; likely cause: {details[0]}.",
                               "Keep the system prompt, tools, MCP servers and model unchanged mid-session; make those changes before you start, or in a new session.")
            elif cause == "deferred_tool_load":
                events, extra = details[:-1], details[-1]
                unused = sorted({n for d in events for n in d["unused"]})
                reloaded = sorted({n for d in events for n in d["reloaded"]})
                overlap = sorted({n for d in events for n in d["overlap"]})
                parts = [f"{len(unused)} of {sum(d['loaded'] for d in events)} loaded deferred tool schemas were never called afterwards"
                         if unused else "", f"{len(reloaded)} loaded again while still in context" if reloaded else "",
                         f"{len(overlap)} duplicate another MCP server's tool" if overlap else ""]
                detail = "; ".join(p for p in parts if p) + f" ({', '.join((overlap or reloaded or unused)[:3])})."
                if extra["shared"]:
                    detail += f" Observation: {len(extra['shared'])} tool(s) were loaded independently by several subagents."
                if overlap and extra["pairs"]:
                    a, b = extra["pairs"][0]
                    fix = (f"Deduplicate the MCP install (plugin vs user config): {a} / {b}." if _same_install(a, b) else
                           f"Remove one of the overlapping servers {a} / {b}.")
                else:
                    fix = "Use select:<name> instead of keyword search."
            else:
                detail, fix = (f"{sum(details):,} input tokens were billed without a cache read or write.",
                               "Keep repeated prompt material in a cacheable prefix.")
            tier = "inferred" if inferred else TIERS[cause]
            top = sorted(evidence, key=lambda e: e["usd"], reverse=True)[:5]
            findings.append({"cause": cause, "session_id": sid, "usd": usd, "detail": detail, "fix": fix,
                             "tier": tier, "attribution": provenance.attribution(tier), "advice": f"{tier.capitalize()}: {fix}",
                             "evidence": top, "session_usd": total})
    return sorted(findings, key=lambda finding: finding["usd"], reverse=True)
