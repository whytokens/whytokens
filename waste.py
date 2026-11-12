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
# resident_output claims min(next cache write, estimated tokens of the previous turn's tool
# results); the rest of that write (assistant text, tool arguments, retained thinking, the next
# user message) stays unattributed. A call whose result size is unknown (NULL result_bytes, or no
# tool_results row) claims nothing and is counted in the finding's detail, never read as 0 or as
# the whole write.
# ponytail: one flat ratio, an ESTIMATE (prose/code run ~3-4 bytes per token). Image, PDF and other
# base64 blocks are stored as compact JSON bytes and inflate far past their real token cost, so for
# those the estimate is a ceiling; the next write still caps it. Per-content-type ratios if that matters.
RESULT_BYTES_PER_TOKEN = 3.5


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


_WRITERS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _tool_info(row, seen):
    """(has a tool call, paths re-read). ``seen`` holds (path, offset, limit) of Reads still in context;
    a write to a path drops it, so a re-read after an edit is a fair re-read. Rows stored before
    offset/limit were kept read as (path, None, None), the old whole-file behaviour."""
    calls = _calls(row)
    repeated, idx = [], []
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        inp = call.get("input") or {}
        if call.get("name") in _WRITERS:
            path = inp.get("file_path") or inp.get("notebook_path")
            seen.difference_update({k for k in seen if k[0] == path})
            continue
        if call.get("name") != "Read":
            continue
        path = inp.get("file_path")
        key = (path, inp.get("offset"), inp.get("limit"))
        if key in seen:
            repeated.append(path)
            idx.append(i)
        if path:
            seen.add(key)
    return bool(calls or row["tool_name"]), repeated, idx


def _aligned(row, sizes, by_id=None):
    """Estimated result tokens per call of ``row`` (None = unknown). A call is joined to its tool_results
    row by tool_use id. Rows stored before ids were kept fall back to position (tool_results rows are
    written in tool_use order) and only when the rows line up with the calls by count and name."""
    calls, res = _calls(row), sizes.get(row["message_id"], ())
    by_id = by_id or {}
    positional = len(res) == len(calls) and all(isinstance(c, dict) and c.get("name") == r[0] for c, r in zip(calls, res))
    out = []
    for i, c in enumerate(calls):
        cid = c.get("id") if isinstance(c, dict) else None
        n = by_id.get(cid) if cid else (res[i][1] if positional else None)
        out.append(None if n is None else n / RESULT_BYTES_PER_TOKEN)
    return out


def _repeat_claim(claims, turn, creation, prev, unknown):
    """Claim the next write for repeated reads, only up to the estimated result tokens of the repeated
    calls themselves. A repeat of unknown size claims nothing (and is counted in ``unknown``). Returns
    False when nothing was claimed, so the caller can attribute the write as resident output instead."""
    sized = [prev["aligned"][i] if i < len(prev["aligned"]) else None for i in prev["rep_idx"]]
    known = [n for n in sized if n is not None]
    est = min(creation, sum(known))
    if est < 1:
        return False
    unknown[0] += len(sized) - len(known)
    _claim(claims, "repeated_reads", turn, round(est), (prev["repeated"], round(est)), "Read")
    rest = creation - round(est)  # the rest of the write belongs to the message's other, known results
    if rest >= 1:
        # all of the message's results, not just its kept calls: a streamed message keeps only its last call
        others, missing = list(prev["results"][0]), prev["results"][1] - (len(sized) - len(known))
        for n in known:
            if ("Read", n) in others:
                others.remove(("Read", n))
        _resident(claims, turn, rest, {"results": (others, max(missing, 0))}, unknown)
    return True


def _cost(row, inp=0, creation=0):
    h1 = 0
    if creation:  # a claim of part of a write carries the same share of its 1h-tier tokens, so split claims never exceed the write's cost
        total, h1 = row["cache_creation_tokens"] or 0, row["cache_creation_1h_tokens"] or 0
        h1 = h1 * min(creation, total) / total if total else h1
    return pricing.calc_cost(row["model"], inp, 0, 0, creation, row["timestamp"], h1) * row["mult"]


def _claim(claims, cause, turn, tokens, detail, tool, inp=False):
    usd = _cost(turn, inp=tokens) if inp else _cost(turn, creation=tokens)
    claims[cause][0] += usd
    claims[cause][1].append(detail)
    claims[cause][2].append({"at": turn["timestamp"], "tokens": tokens, "usd": usd, "tool": tool,
                              "message_id": turn["message_id"], "agent_id": turn["agent_id"]})


def _results(row, sizes):
    """([(tool, est_tokens)] for this turn's calls whose result size is known, unknown call count)."""
    known = [(name or "tool", n / RESULT_BYTES_PER_TOKEN)
             for name, n in sizes.get(row["message_id"], ()) if n is not None]
    calls = max(len(_calls(row)), len(sizes.get(row["message_id"], ())), 1 if row["tool_name"] else 0)
    return known, calls - len(known)


OVERSIZED_TOKENS = 5_000  # a tool result at or above this many estimated tokens counts as oversized
_RULES = {  # action id -> (title verb, rule text); rule text is draft, owner approves
    "read_ranges": ("Read whole files", "Before reading a file over 500 lines, Grep for the part you need, "
                    "then Read only that range with offset and limit."),
    "bash_tail": ("Print long command output", "Pipe any command that can print more than 50 lines through "
                  "`tail` or a summary; never dump full logs or test output."),
    "search_limits": ("Search with full content", "Search with files-with-matches or a head limit first; "
                      "ask for content only on the files you will open."),
    "mcp_text": ("Pull big MCP results", "From `{server}`, prefer text results over screenshots or full dumps; "
                 "request only the fields needed."),
    "compact_before_break": ("Idle gaps on the main thread", "Before stepping away for more than 5 minutes, run /compact; "
                             "after 4 hours, start a new session from the handoff file."),
    "subagent_waits": ("Subagent waits past the cache TTL", "A subagent never sleeps or polls longer than 4 minutes; "
                       "start long jobs in the background from the main thread."),
    "small_jobs_inline": ("Short subagent jobs", "Do not spawn a subagent for a job under ~10 tool calls."),
}
SMALL_JOB_TURNS = 10  # a subagent lane under this many turns is a job the main thread could have done


def idle_gap_action(evidence, gaps, types=None):
    """Rule for idle gaps. ``evidence``/``gaps`` are the idle_gap claims and their (gap, tokens, warm)
    details, index-aligned. Mostly-subagent gaps get subagent_waits, else compact_before_break.
    est_usd is what the gaps cost minus what a warm cache would have billed: an upper bound."""
    side = sum(bool(e["agent_id"]) for e in evidence) * 2 > len(evidence)
    rid = "subagent_waits" if side else "compact_before_break"
    verb, rule = _RULES[rid]
    calls = sorted(({"message_id": e["message_id"], "at": e["at"], "minutes": round(g.total_seconds() / 60),
                     "tokens": n, "usd": e["usd"], "warm_usd": w,
                     "side": bool(e["agent_id"]), "agent_type": (types or {}).get(e["agent_id"]) if e["agent_id"] else None} for e, (g, n, w) in zip(evidence, gaps)),
                   key=lambda c: c["usd"], reverse=True)
    return {"id": rid, "title": f"{verb}: {len(calls)} gap{'' if len(calls) == 1 else 's'}", "rule": rule, "calls": calls[:5],
            "est_usd": sum(c["usd"] - c["warm_usd"] for c in calls),
            "est_basis": "Estimate, upper bound: what the gaps cost minus what the same tokens would have billed as cache reads."}


def small_jobs_action(evidence, turns_by_lane, types):
    """Rule for subagent preambles, or None when no lane ran under SMALL_JOB_TURNS turns.
    Lanes without an agent_id cannot be told apart, so they never count as small."""
    small = [dict(message_id=e["message_id"], at=e["at"], agent_type=types.get(e["agent_id"]),
                  turns=turns_by_lane[e["agent_id"]], usd=e["usd"])
             for e in evidence if e["agent_id"] and turns_by_lane.get(e["agent_id"], SMALL_JOB_TURNS) < SMALL_JOB_TURNS]
    if not small:
        return None
    verb, rule = _RULES["small_jobs_inline"]
    small.sort(key=lambda c: c["usd"], reverse=True)
    lanes = {e["agent_id"] for e in evidence if e["agent_id"]}
    loose = sum(not e["agent_id"] for e in evidence)  # unidentified writes: not countable as lanes
    return {"id": "small_jobs_inline", "title": f"{verb}: {len(small)} of {len(lanes)} subagents ran under {SMALL_JOB_TURNS} turns"
                                                + (f" (+{loose} unidentified cold writes not counted)" if loose else ""),
            "rule": rule, "calls": small[:5], "est_usd": sum(c["usd"] for c in small),
            "est_basis": "Estimate, upper bound: the whole preamble write of each short subagent, assuming none of it would be needed inline."}


def _rule_for(tool):
    """(action id, group key) for a tool name, or None when no rule covers it."""
    tool = tool or ""
    if tool == "Read":
        return "read_ranges", tool
    if tool == "Bash":
        return "bash_tail", tool
    if tool in ("Grep", "Glob"):
        return "search_limits", "search"
    m = re.match(r"mcp__(.+?)__", tool)
    return ("mcp_text", m.group(1)) if m else None


def next_step_action(calls):
    """The one rule for the habit that kept the most oversized tool output in context, or None.

    ``calls``: dicts {message_id, at, tool, file, result_tokens, prompt_seq, excess_usd}, one per tool
    call whose result size is KNOWN (an unknown size is never passed in as zero). The oversized
    calls of each rule group are summed; the largest group wins. ``excess_usd`` is the cache-write
    price of the tokens above the floor, so est_usd is a lower bound, never a saving."""
    groups = {}
    for c in calls:
        rule = _rule_for(c["tool"])
        if rule and c["result_tokens"] >= OVERSIZED_TOKENS:
            groups.setdefault(rule, []).append(c)
    if not groups:
        return None
    (rid, key), big = max(groups.items(), key=lambda kv: sum(c["result_tokens"] for c in kv[1]))
    top = sorted(big, key=lambda c: c["result_tokens"], reverse=True)[:5]
    verb, rule = _RULES[rid]
    avg = sum(c["result_tokens"] for c in big) / len(big)
    return {"id": rid, "title": f"{verb} {len(big)} time{'' if len(big) == 1 else 's'}, ~{round(avg / 1000)}K tokens each",
            "rule": rule.format(server=key),
            "calls": [{k: c[k] for k in ("message_id", "at", "tool", "file", "result_tokens", "prompt_seq")} for c in top],
            "est_usd": sum(c["excess_usd"] for c in big),
            "est_basis": f"Estimate, lower bound: the {len(big)} oversized results cut to {OVERSIZED_TOKENS:,} tokens, "
                         "at the cache-write rate only. Not a saving."}


def _resident(claims, turn, creation, prev, unknown):
    """Claim the next write only up to the previous turn's estimated result tokens."""
    known, missing = prev["results"]
    unknown[0] += missing
    est = sum(n for _, n in known)
    claim = min(creation, est)
    if claim < 1:
        return
    scale = claim / est  # per-call figures capped with the write, so the ranking sums to the claim
    per_call = [(name, n * scale) for name, n in known]
    _claim(claims, "resident_output", turn, round(claim), per_call, max(per_call, key=lambda p: p[1])[0])


def _codex_resident(claims, turn, state):
    """Codex resident_output: context growth (input + cache_read) over the previous request, when
    that request called a tool. Charged at the fresh-input rate, capped at the turn's fresh input.
    Not capped by result size: Codex records none (no tool_results rows), so growth is all it has."""
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


def _action_calls(turns, sizes):
    """Every call of a session with a known result size, for next_step_action. A file basename is
    given only when the turn made one call of that tool (tool_calls carry no call id to match on)."""
    out = []
    last = {t["agent_id"]: i for i, t in enumerate(turns) if not _synthetic(t)}  # a lane's last turn: nothing re-reads its results
    for i, turn in enumerate(turns):
        if _synthetic(turn) or last[turn["agent_id"]] == i:
            continue
        known = [(name, n) for name, n in sizes.get(turn["message_id"], ()) if n is not None]
        names = [c.get("name") for c in _calls(turn) if isinstance(c, dict)]
        for name, n in known:
            tokens = n / RESULT_BYTES_PER_TOKEN
            path = None
            if names.count(name) == 1:
                path = next(((c.get("input") or {}).get("file_path") for c in _calls(turn)
                             if isinstance(c, dict) and c.get("name") == name), None)
            out.append({"message_id": turn["message_id"], "at": turn["timestamp"], "tool": name or "tool",
                        "file": re.split(r"[\\/]", str(path))[-1] if path else None, "result_tokens": round(tokens),
                        "prompt_seq": turn["prompt_seq"],
                        "excess_usd": _cost(turn, creation=max(0, tokens - OVERSIZED_TOKENS))})
    return out


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
                         {prompt_seq} AS prompt_seq, {message_id} AS message_id, {agent_id} AS agent_id, {speed} AS speed, {mult} AS mult
                  FROM turns"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        sql = sql.format(mult=pricing.price_mult_sql(cols),
                         **{c: c if c in cols else "NULL" for c in ("message_id", "agent_id", "speed", "prompt_seq")})
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
        sizes, by_id = {}, {}  # message_id -> [(tool_name, result_bytes or None)], one per call; call_id -> bytes
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='tool_results'").fetchone():
            for mid, name, n, cid in conn.execute("SELECT message_id, tool_name, result_bytes, call_id FROM tool_results "
                                             "WHERE message_id IS NOT NULL ORDER BY rowid"):
                sizes.setdefault(mid, []).append((name, n))
                by_id[cid] = n
        types = dict(conn.execute("SELECT agent_id, agent_type FROM agents")) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='agents'").fetchone() else {}
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
        unknown = [0]  # tool calls of unknown result size that resident_output could not count
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
                lane = lanes.setdefault(turn["agent_id"], {"seen": set(), "prev": None, "preamble": False,
                                                           "at": None, "ttl": 300})
                compact = turn["is_compact_summary"]  # same guard as the main chain below
                if compact:
                    lane["seen"].clear()  # compaction dropped what the lane had read; this turn's own reads are in context
                has_tool, repeated, rep_idx = _tool_info(turn, lane["seen"])
                if compact:
                    has_tool, repeated, rep_idx = False, [], []
                prev = lane["prev"]
                if creation and not lane["preamble"]:
                    lane["preamble"] = True
                    _claim(claims, "subagent_preamble", turn, creation, creation, "Agent")
                elif creation and lane["at"] and (lgap := _gap(lane["at"], turn["timestamp"])) and lgap.total_seconds() >= lane["ttl"]:
                    # the lane polled past its own prefix's TTL: an idle gap, not the previous tool's output
                    warm = pricing.calc_cost(turn["model"], 0, 0, creation, 0, turn["timestamp"]) * turn["mult"]
                    _claim(claims, "idle_gap", turn, creation, (lgap, creation, warm), None)
                elif creation and not compact and prev and prev["repeated"] and _repeat_claim(claims, turn, creation, prev, unknown):
                    pass
                elif creation and not compact and prev and prev["has_tool"]:
                    _resident(claims, turn, creation, prev, unknown)
                lane["prev"] = {"has_tool": has_tool, "repeated": repeated, "rep_idx": rep_idx, "names": [c.get("name") if isinstance(c, dict) else None for c in _calls(turn)], "aligned": _aligned(turn, sizes, by_id), "results": _results(turn, sizes)}
                lane["ttl"] = 3600 if (turn["cache_creation_1h_tokens"] or 0) > 0 or (
                    (turn["cache_read_tokens"] or 0) and lane["ttl"] > 300) else 300
                lane["at"] = turn["timestamp"]
                previous_turn = turn
                continue
            compact = turn["is_compact_summary"]
            if compact:
                seen_paths.clear()  # compaction dropped what was read; this turn's own reads are in context
            has_tool, repeated, rep_idx = _tool_info(turn, seen_paths)
            if compact:
                has_tool, repeated, rep_idx = False, [], []
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
                elif not compact and previous_main["repeated"] and _repeat_claim(claims, turn, creation, previous_main, unknown):
                    pass
                elif not compact and previous_main["has_tool"]:
                    _resident(claims, turn, creation, previous_main, unknown)
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
                             "repeated": repeated, "rep_idx": rep_idx, "names": [c.get("name") if isinstance(c, dict) else None for c in _calls(turn)], "aligned": _aligned(turn, sizes, by_id), "results": _results(turn, sizes)}
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
                pairs = [p for d in details for p in (d if isinstance(d, list) else [d])]  # Claude: per call
                by_tool = {}
                for name, n in pairs:
                    by_tool[name] = by_tool.get(name, 0) + n
                ranked = sorted(((round(n), name) for name, n in by_tool.items()), reverse=True)
                names = ", ".join(f"{name} ({tokens:,})" for tokens, name in ranked[:3])
                if len(ranked) > 3:
                    names += f" (+{len(ranked) - 3} more)"
                total_tokens = sum(e["tokens"] for e in evidence)
                detail = (f"{names} caused {total_tokens:,} input tokens of context growth." if inferred else
                          f"{names} kept an estimated {total_tokens:,} cache-write tokens of tool results in context"
                          f" ({RESULT_BYTES_PER_TOKEN} bytes per token)." +
                          (f" {unknown[0]:,} tool call{'' if unknown[0] == 1 else 's'} of unknown result size not counted."
                           if unknown[0] else ""))
                fix = (
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
                               ("Keep subagent waits under the cache TTL, or run long jobs in the background from the main thread."
                                if sum(bool(e["agent_id"]) for e in evidence) * 2 > len(evidence) else
                                "Start a fresh session instead of resuming a stale one; re-onboard from the handoff file."
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
            action = None
            if inferred:
                pass
            elif cause == "resident_output":
                action = next_step_action(_action_calls(turns, sizes))
            elif cause == "idle_gap":
                action = idle_gap_action(evidence, details, types)  # all gaps, not the top five
            elif cause == "subagent_preamble":
                lane_turns = {}
                for t in turns:
                    if t["is_sidechain"] and t["agent_id"] and not _synthetic(t):
                        lane_turns[t["agent_id"]] = lane_turns.get(t["agent_id"], 0) + 1
                action = small_jobs_action(evidence, lane_turns, types)
            findings.append({"cause": cause, "session_id": sid, "usd": usd, "detail": detail, "fix": fix,
                             "tier": tier, "attribution": provenance.attribution(tier), "advice": f"{tier.capitalize()}: {fix}",
                             "evidence": top, "session_usd": total, "action": action})
    return sorted(findings, key=lambda finding: finding["usd"], reverse=True)
