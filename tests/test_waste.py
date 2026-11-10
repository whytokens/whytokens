"""test_waste.py - falsification harness for the ``waste`` report.

Contract this file pins:

    waste.report(db_path, session_id=None) -> list[dict]
        each finding: {"cause", "session_id", "usd", "detail", "fix"}
        sorted by usd desc; only causes whose usd >= max($0.01, 5% of that
        session's cost) are reported, so a clean session reports [].

Causes (all derivable from ``turns``; ctx(turn) = input + cache_read + cache_creation):
  resident_output    context growth after a tool turn, carried by every later main-chain
                     turn. EXCLUDES growth claimed by repeated_reads (disjoint).
  repeated_reads     growth after the 2nd+ Read of the SAME file_path in one session.
  subagent_preamble  the FIRST cache_creation of each subagent (agent_id; a cold read
                     starts a lane when agent_id is NULL). Later sidechain writes follow
                     the resident_output / repeated_reads rules within that agent.
  idle_gap           cache re-creation on the first turn after a >= 30-minute gap.
  uncached_input     input_tokens billed at full price that a cache would have served.

Method (a verifier is not done until it has been seen to fail): one known-good
session must report [], one mutant per detector must be caught with the right top cause,
one mixed session must rank the bigger cause first, and no session may have its findings
sum to more than it actually cost (double-counting guard). Fixtures go through the real
scanner path (JSONL -> scan()), not hand-inserted rows.
"""

import importlib
import json
import shutil
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing
from scanner import scan

MODEL = "claude-sonnet-4-6"
T0 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
CAUSES = ("resident_output", "repeated_reads", "subagent_preamble", "idle_gap", "uncached_input",
          "cache_miss_rebuild")
KEYS = {"cause", "session_id", "usd", "detail", "fix", "tier", "advice", "evidence", "session_usd"}


def _ts(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(sid, n, ts, inp=200, read=0, create=400, out=300, tools=(), side=False, agent=None, advisor=0,
         mid=True, compact=False):
    """One assistant record in the Claude Code envelope the scanner parses."""
    content = [{"type": "text", "text": "..."}] + [
        {"type": "tool_use", "id": f"tu-{sid}-{n}-{i}", "name": name, "input": arg}
        for i, (name, arg) in enumerate(tools)]
    return json.dumps({
        "type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/home/dev/acme/app",
        "isSidechain": side, "version": "2.1.220", **({"agentId": agent} if agent is not None else {}),
        **({"isCompactSummary": True} if compact else {}),
        "message": {**({"id": f"msg-{sid}-{n}"} if mid else {}), "model": MODEL, "content": content,
                    "stop_reason": "tool_use" if tools else "end_turn",
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": create,
                              # ``advisor`` adds an advisor iteration: the scanner mints a
                              # synthetic ``advisor:`` turn (no cache, tool_name "advisor")
                              **({"iterations": [{"type": "message"},
                                                 {"type": "advisor_message", "input_tokens": advisor,
                                                  "output_tokens": 2_000}]} if advisor else {})}},
    })


def _main_chain(sid, n_turns, base, tool_at=None, growth=0, gap_after=None):
    """A main-chain session: turn 1 writes ``base``, later turns read it back.
    ``tool_at`` {turn_no: [(tool, input)]} adds tool calls; the NEXT turn grows by
    ``growth[turn_no]``. ``gap_after`` = turn number followed by a 45-minute idle gap."""
    tool_at, growth = tool_at or {}, growth or {}
    lines, ctx, minute, n = [], base, 0, 0
    for t in range(1, n_turns + 1):
        n += 1
        if t == 1:
            lines.append(_rec(sid, n, _ts(minute), read=0, create=base, tools=tool_at.get(t, ())))
        elif gap_after is not None and t == gap_after + 1:
            lines.append(_rec(sid, n, _ts(minute), read=0, create=ctx, tools=tool_at.get(t, ())))
        else:
            grow = growth.get(t - 1, 0) + 400
            lines.append(_rec(sid, n, _ts(minute), read=ctx, create=grow, tools=tool_at.get(t, ())))
            ctx += grow
        minute += 45 if (gap_after is not None and t == gap_after) else 3
    return lines


READ_BIG = ("Read", {"file_path": "/src/big.py"})

FIXTURES = {
    # known-good: flat context, no tools, no gaps, no sidechain -> must report []
    "clean": _main_chain("clean", 6, base=20_000),
    # one big Bash output carried for the rest of the session
    "resident": _main_chain("resident", 10, base=20_000,
                            tool_at={2: [("Bash", {"command": "npm test"})]}, growth={2: 60_000}),
    # /src/big.py read once (resident) then re-read four times (repeated_reads dominates 4:1)
    "reread": _main_chain("reread", 12, base=20_000,
                          tool_at={t: [READ_BIG] for t in (2, 4, 6, 8, 10)},
                          growth={t: 30_000 for t in (2, 4, 6, 8, 10)}),
    # 45-minute gap after turn 5: turn 6 rewrites the whole context
    "idle": _main_chain("idle", 10, base=60_000, gap_after=5),
    # no cache at all: every turn bills 40k input tokens at full price
    "uncached": [_rec("uncached", t, _ts(3 * (t - 1)), inp=40_000, read=0, create=0)
                 for t in range(1, 11)],
    # mixed: small idle re-creation early (~11% of cost) + four re-reads (~40%) -> reads rank first
    "mixed": _main_chain("mixed", 14, base=40_000, gap_after=2,
                         tool_at={t: [READ_BIG] for t in (3, 5, 7, 9, 11)},
                         growth={t: 30_000 for t in (3, 5, 7, 9, 11)}),
}


def _subagent_session():
    """8 main turns; turns 2-6 each dispatch an Agent whose 3 sidechain turns write a
    45k preamble then read it back. Main-chain growth per dispatch is tiny (1.5k)."""
    sid, lines, ctx, n = "subagent", [], 10_000, 0
    for t in range(1, 9):
        n += 1
        minute = 3 * (t - 1)
        dispatch = [("Agent", {"prompt": "sweep"})] if 2 <= t <= 6 else ()
        if t == 1:
            lines.append(_rec(sid, n, _ts(minute), read=0, create=ctx, tools=dispatch))
        else:
            grow = 1_500 if 3 <= t <= 7 else 400
            lines.append(_rec(sid, n, _ts(minute), read=ctx, create=grow, tools=dispatch))
            ctx += grow
        if dispatch:
            for k, (read, create) in enumerate(((0, 45_000), (45_000, 300), (45_300, 300))):
                n += 1
                lines.append(_rec(sid, n, _ts(minute, 30 * (k + 1)), read=read, create=create, side=True))
    return lines


FIXTURES["subagent"] = _subagent_session()


def _busy_gap_session():
    """Two main turns 40 minutes apart, but sidechain turns fill the gap (an Agent was
    running the whole time). The main context's cache still expired and was rewritten,
    yet the user was NOT idle: idle_gap must not fire and its fix must not be offered."""
    sid, ctx, lines = "busy_gap", 30_000, []
    lines.append(_rec(sid, 1, _ts(0), read=0, create=ctx, tools=[("Agent", {"prompt": "long sweep"})]))
    for k, m in enumerate((5, 15, 25, 35)):
        lines.append(_rec(sid, 2 + k, _ts(m), read=8_000 if k else 0, create=300 if k else 8_000, side=True))
    lines.append(_rec(sid, 6, _ts(40), read=0, create=ctx + 1_000))
    lines.append(_rec(sid, 7, _ts(43), read=ctx + 1_000, create=400))
    return lines


FIXTURES["busy_gap"] = _busy_gap_session()

PREAMBLE, GROWTH = 30_000, 20_000


def _subagent_growth_session(sid, ids=True):
    """Two subagents run interleaved. Each writes a 30k preamble once, then calls its own
    tool (a1 Bash, a2 Grep) every turn and grows its OWN context 20k per turn for 9 turns.
    Only the two first writes are preamble; the growth is tool output kept in that
    agent's context, so each tool must be charged exactly its own agent's growth.
    a1's 4th turn and the last main turn also call the advisor: the synthetic advisor
    rows must not split a lane, overwrite tool state, or count as uncached input.
    ``ids=False`` drops agentId (NULL agent_id), ``ids=""`` writes an empty one: either way
    the agents cannot be told apart, so only the cold preamble writes may be claimed."""
    lines = [_rec(sid, 1, _ts(0), read=0, create=10_000, tools=[("Agent", {"prompt": "a"}),
                                                                 ("Agent", {"prompt": "b"})])]
    n, ctx = 1, {"a1": PREAMBLE, "a2": PREAMBLE}
    for k in range(10):
        for j, (agent, tool) in enumerate((("a1", "Bash"), ("a2", "Grep"))):
            n += 1
            ts = _ts(1 + k, 20 * j)
            call = [(tool, {"command": f"step {k}"})]
            advisor = 50_000 if (agent, k) == ("a1", 3) else 0
            if k == 0:
                lines.append(_rec(sid, n, ts, read=0, create=PREAMBLE, tools=call, side=True,
                                  agent=agent if ids is True else ("" if ids == "" else None)))
            else:
                lines.append(_rec(sid, n, ts, read=ctx[agent], create=GROWTH, tools=call, side=True,
                                  agent=agent if ids is True else ("" if ids == "" else None), advisor=advisor))
                ctx[agent] += GROWTH
    lines.append(_rec(sid, n + 1, _ts(12), read=10_000, create=400, advisor=50_000))
    return lines


FIXTURES["sub_growth"] = _subagent_growth_session("sub_growth")
FIXTURES["sub_growth_noid"] = _subagent_growth_session("sub_growth_noid", ids=False)
FIXTURES["sub_growth_blankid"] = _subagent_growth_session("sub_growth_blankid", ids="")
# Two interleaved agents both Read /src/x.py on every turn. Only a1 reads it a second time
# within its OWN lane, so only a1's growth after that re-read is repeated_reads; a2's single
# read is ordinary resident output, never "repeated" because a1 read the same path.
READ_X = ("Read", {"file_path": "/src/x.py"})
FIXTURES["sub_reads"] = [_rec("sub_reads", 1, _ts(0), read=0, create=10_000,
                              tools=[("Agent", {"prompt": "a"}), ("Agent", {"prompt": "b"})])] + [
    _rec("sub_reads", 2 + 2 * k + j, _ts(1 + k, 20 * j), read=0 if k == 0 else PREAMBLE + GROWTH * (k - 1),
         create=PREAMBLE if k == 0 else GROWTH, tools=[READ_X] if (agent == "a1" or k == 0) else [],
         side=True, agent=agent)
    for k in range(3) for j, agent in enumerate(("a1", "a2"))]
# One subagent compacts mid-run: its 150k rewrite is a compaction, not output of the
# Bash call before it. As on the main chain, the compaction turn's own tool call is not
# carried forward either, so Bash is charged exactly the one 20k write before compaction.
FIXTURES["sub_compact"] = [_rec("sub_compact", 1, _ts(0), read=0, create=10_000, tools=[("Agent", {"prompt": "a"})])] + [
    _rec("sub_compact", 2 + k, _ts(1 + k), read=r, create=c, tools=[("Bash", {"command": str(k)})],
         side=True, agent="a1", compact=(k == 2))
    for k, (r, c) in enumerate(((0, PREAMBLE), (PREAMBLE, GROWTH), (0, 150_000), (150_000, GROWTH)))]
# A cached main chain whose last message has no message.id and calls the advisor: the
# scanner stores that advisor row with an EMPTY message_id (no prefix to recognise it by),
# and its 50k uncached input must still not be reported as uncached_input.
FIXTURES["advisor_noid"] = _main_chain("advisor_noid", 6, base=20_000)[:-1] + [
    _rec("advisor_noid", 6, _ts(15), read=22_000, create=400, advisor=50_000, mid=False)]

# Subagent transcript written under <session>/subagents/, so the scanner also mints its
# AgentSummary row. a1 writes a 30k preamble then grows 20k after each of 5 Bash calls.
SUBAGENT_FILES = {("summary", "agent-a1"): [
    _rec("summary", 100 + k, _ts(1 + k), read=0 if k == 0 else PREAMBLE + GROWTH * (k - 1),
         create=PREAMBLE if k == 0 else GROWTH, tools=[("Bash", {"command": f"s{k}"})], side=True, agent="a1")
    for k in range(6)]}
FIXTURES["summary"] = [_rec("summary", 1, _ts(0), read=0, create=10_000, tools=[("Agent", {"prompt": "a"})]),
                       _rec("summary", 2, _ts(8), read=10_000, create=400)]
EXPECTED_TOP = {"resident": "resident_output", "reread": "repeated_reads",
                "subagent": "subagent_preamble", "idle": "idle_gap",
                "uncached": "uncached_input", "mixed": "repeated_reads",
                "sub_growth": "resident_output", "sub_growth_noid": "subagent_preamble",
                "sub_growth_blankid": "subagent_preamble"}


class _Scanned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for (sid, stem), lines in SUBAGENT_FILES.items():
            (proj / sid / "subagents").mkdir(parents=True)
            (proj / sid / "subagents" / f"{stem}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        scan(projects_dir=proj.parent, db_path=cls.db, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _rows(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _session_cost(self, sid):
        return sum(pricing.calc_cost(m, i, o, cr, cc, ts, cc1h) for m, i, o, cr, cc, ts, cc1h in self._rows(
            "SELECT model,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,"
            "timestamp,cache_creation_1h_tokens FROM turns WHERE session_id=?", sid))


class TestWasteFixtures(_Scanned):
    """The fixtures themselves carry the signals they claim (independent of waste.py)."""

    def test_all_sessions_scanned(self):
        self.assertEqual({r[0] for r in self._rows("SELECT session_id FROM sessions")}, set(FIXTURES))

    def test_reread_has_five_reads_of_one_file(self):
        calls = [json.loads(r[0]) for r in self._rows(
            "SELECT tool_calls FROM turns WHERE session_id='reread' AND tool_calls IS NOT NULL")]
        paths = [c["input"]["file_path"] for tc in calls for c in tc if c["name"] == "Read"]
        self.assertEqual(paths, ["/src/big.py"] * 5)

    def test_subagent_preamble_dominates_its_session(self):
        (side, main), = self._rows(
            "SELECT SUM(CASE WHEN is_sidechain=1 THEN cache_creation_tokens ELSE 0 END),"
            "SUM(CASE WHEN is_sidechain=0 THEN cache_creation_tokens ELSE 0 END)"
            " FROM turns WHERE session_id='subagent'")
        self.assertEqual(side, 5 * 45_000 + 10 * 300)
        self.assertGreater(side, 10 * main)

    def test_idle_gap_is_45_minutes_then_full_recreation(self):
        rows = self._rows("SELECT timestamp,cache_read_tokens,cache_creation_tokens FROM turns"
                          " WHERE session_id='idle' ORDER BY timestamp")
        t5, t6 = datetime.fromisoformat(rows[4][0].replace("Z", "+00:00")), \
            datetime.fromisoformat(rows[5][0].replace("Z", "+00:00"))
        self.assertEqual((t6 - t5), timedelta(minutes=45))
        self.assertEqual(rows[5][1], 0)
        self.assertGreater(rows[5][2], 60_000)

    def test_clean_and_uncached_have_no_tools_or_gaps(self):
        for sid in ("clean", "uncached"):
            self.assertEqual(self._rows("SELECT COUNT(*) FROM turns WHERE session_id=?"
                                        " AND tool_name IS NOT NULL", sid)[0][0], 0)


class TestWasteReport(_Scanned):
    """Red until waste.py exists; each test is one thing the report must get right."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        real = importlib.import_module("waste")  # ModuleNotFoundError = loud red
        cls.real_waste = real
        # fixtures are small: the detector tests run at the old 5% floor, the cutoff has its own tests
        cls.waste = types.SimpleNamespace(
            report=lambda db, session_id=None: real.report(db, session_id, min_share=.05))

    def test_default_cutoff_is_ten_percent_and_configurable(self):
        w, db = self.real_waste, self.db
        self.assertEqual(w.MIN_SHARE, .10)  # fixtures straddle only 8.3%..11%, so pin the default itself
        shares = [(sid, f["cause"], f["usd"] / f["session_usd"]) for sid in EXPECTED_TOP
                  for f in w.report(db, sid, min_share=0)]
        below = [x for x in shares if .05 <= x[2] < .10]
        above = [x for x in shares if x[2] >= .10]
        self.assertTrue(below and above, "fixtures must straddle the 10% line")
        for sid in {x[0] for x in shares}:
            kept = {f["cause"] for f in w.report(db, sid)}
            self.assertEqual(kept, {c for s_, c, sh in shares if s_ == sid and sh >= .10}, sid)
        sid, cause, share = below[0]  # 9%-ish: dropped by default, kept once the cutoff is lowered
        self.assertNotIn(cause, {f["cause"] for f in w.report(db, sid)})
        self.assertIn(cause, {f["cause"] for f in w.report(db, sid, min_share=share - .001)})
        sid, cause, share = above[0]  # 11%-ish: kept by default, dropped once the cutoff is raised
        self.assertIn(cause, {f["cause"] for f in w.report(db, sid)})
        self.assertNotIn(cause, {f["cause"] for f in w.report(db, sid, min_share=share + .001)})

    def test_known_good_session_reports_nothing(self):
        self.assertEqual(self.waste.report(self.db, session_id="clean"), [])

    def test_each_mutant_is_caught_with_the_right_top_cause(self):
        for sid, cause in EXPECTED_TOP.items():
            with self.subTest(session=sid):
                findings = self.waste.report(self.db, session_id=sid)
                self.assertTrue(findings, f"{sid}: no finding at all")
                self.assertEqual(findings[0]["cause"], cause)

    def test_mixed_session_ranks_reads_above_idle(self):
        causes = [f["cause"] for f in self.waste.report(self.db, session_id="mixed")]
        self.assertIn("idle_gap", causes)
        self.assertLess(causes.index("repeated_reads"), causes.index("idle_gap"))

    def test_subagent_preamble_is_each_agents_first_write_only(self):
        # Regression: every sidechain cache write used to count as preamble.
        expected = pricing.calc_cost(MODEL, 0, 0, 0, 2 * PREAMBLE, _ts(1))
        for sid in ("sub_growth", "sub_growth_noid", "sub_growth_blankid"):
            with self.subTest(session=sid):
                findings = {f["cause"]: f for f in self.waste.report(self.db, session_id=sid)}
                self.assertAlmostEqual(findings["subagent_preamble"]["usd"], expected, places=9)
                self.assertIn(f"{2 * PREAMBLE:,}", findings["subagent_preamble"]["detail"])
                self.assertNotIn("uncached_input", findings)
        findings = {f["cause"]: f for f in self.waste.report(self.db, session_id="sub_growth")}
        detail = findings["resident_output"]["detail"]
        self.assertIn(f"Bash ({9 * GROWTH:,})", detail)
        self.assertIn(f"Grep ({9 * GROWTH:,})", detail)
        self.assertNotIn("advisor", detail)
        # NULL agent_id: growth cannot be pinned to an agent, so none is charged to a tool
        for sid in ("sub_growth_noid", "sub_growth_blankid"):
            causes = {f["cause"]: f for f in self.waste.report(self.db, session_id=sid)}
            self.assertNotIn("resident_output", causes, sid)
            self.assertAlmostEqual(causes["subagent_preamble"]["usd"], expected, places=9, msg=sid)

    def test_subagent_repeated_reads_are_lane_local(self):
        findings = {f["cause"]: f for f in self.waste.report(self.db, session_id="sub_reads")}
        self.assertIn(f"added {GROWTH:,} cache-write tokens", findings["repeated_reads"]["detail"])
        self.assertIn("x.py", findings["repeated_reads"]["detail"])
        self.assertIn(f"Read ({2 * GROWTH:,})", findings["resident_output"]["detail"])

    def test_subagent_compaction_is_not_tool_output(self):
        self.assertEqual(self._rows("SELECT SUM(is_compact_summary) FROM turns WHERE session_id='sub_compact'"),
                         [(1,)])
        detail = {f["cause"]: f for f in self.waste.report(self.db, session_id="sub_compact")}["resident_output"]["detail"]
        self.assertIn(f"Bash ({GROWTH:,}) caused {GROWTH:,} cache-write tokens", detail)

    def test_advisor_row_without_message_id_is_not_uncached_input(self):
        ids = self._rows("SELECT message_id FROM turns WHERE session_id='advisor_noid' AND tool_name='advisor'")
        self.assertEqual(ids, [("",)])  # the fixture really has the unprefixed shape
        self.assertEqual(self.waste.report(self.db, session_id="advisor_noid"), [])

    def test_agent_summary_mid_lane_does_not_reset_tool_state(self):
        # An incremental rescan can leave the AgentSummary row earlier than the agent's
        # later turns. Move it mid-lane: Bash must still be charged all 5 growth writes.
        db = str(Path(self.tmp) / "summary.db")
        shutil.copy(self.db, db)
        conn = sqlite3.connect(db)
        try:
            (mid_ts,), = conn.execute("SELECT timestamp FROM turns WHERE session_id='summary'"
                                      " AND is_sidechain=1 ORDER BY timestamp LIMIT 1 OFFSET 2").fetchall()
            moved = conn.execute("UPDATE turns SET timestamp=? WHERE message_id LIKE 'agent_summary:%'"
                                 " AND session_id='summary'", (mid_ts,)).rowcount
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(moved, 1)  # the scanner did mint the summary row
        detail = {f["cause"]: f for f in self.waste.report(db, session_id="summary")}["resident_output"]["detail"]
        self.assertIn(f"Bash ({5 * GROWTH:,})", detail)

    def test_db_without_message_id_or_agent_id_columns_still_reports(self):
        db = str(Path(self.tmp) / "old.db")
        shutil.copy(self.db, db)
        conn = sqlite3.connect(db)
        try:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index'"
                                        " AND tbl_name='turns' AND sql IS NOT NULL").fetchall():
                conn.execute(f"DROP INDEX {name}")
            conn.execute("ALTER TABLE turns DROP COLUMN message_id")
            conn.execute("ALTER TABLE turns DROP COLUMN agent_id")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.waste.report(db, session_id="clean"), [])
        self.assertTrue(self.waste.report(db, session_id="resident"))

    def test_idle_gap_prices_compacting_before_the_ttl(self):
        # 61,600 tokens rebuilt at the write rate; kept warm they would have been cache reads
        f = [f for f in self.waste.report(self.db, session_id="idle") if f["cause"] == "idle_gap"][0]
        tokens = 60_000 + 4 * 400
        paid = pricing.calc_cost(MODEL, 0, 0, 0, tokens, _ts(15))
        warm = pricing.calc_cost(MODEL, 0, 0, tokens, 0, _ts(15))
        self.assertAlmostEqual(f["usd"], paid)
        self.assertIn(f"${paid - warm:.2f}", f["detail"])
        self.assertIn(f"${paid:.2f}", f["detail"])
        self.assertNotIn("$", f["fix"])  # fix is shared across a project/cause group in the UI

    def test_gap_filled_by_subagent_work_is_not_idle(self):
        causes = [f["cause"] for f in self.waste.report(self.db, session_id="busy_gap")]
        self.assertNotIn("idle_gap", causes)

    def test_details_name_the_culprit(self):
        top = self.waste.report(self.db, session_id="reread")[0]
        self.assertIn("big.py", top["detail"])
        self.assertNotIn("/src/", top["detail"])   # basenames only
        top = self.waste.report(self.db, session_id="resident")[0]
        self.assertIn("Bash", top["detail"])

    def test_finding_shape_and_ordering(self):
        for sid in FIXTURES:
            findings = self.waste.report(self.db, session_id=sid)
            usds = [f["usd"] for f in findings]
            self.assertEqual(usds, sorted(usds, reverse=True), sid)
            for f in findings:
                self.assertEqual(set(f), KEYS, sid)
                self.assertIn(f["cause"], CAUSES)
                self.assertEqual(f["session_id"], sid)
                self.assertGreater(f["usd"], 0)
                self.assertTrue(f["fix"].strip(), f"{sid}/{f['cause']}: empty fix")

    def test_findings_never_exceed_what_the_session_cost(self):
        for sid in FIXTURES:
            total = sum(f["usd"] for f in self.waste.report(self.db, session_id=sid))
            self.assertLessEqual(total, self._session_cost(sid) + 1e-9, sid)

    def test_whole_db_report_skips_the_clean_session(self):
        sids = {f["session_id"] for f in self.waste.report(self.db)}
        self.assertNotIn("clean", sids)
        self.assertTrue(set(EXPECTED_TOP) <= sids)


if __name__ == "__main__":
    unittest.main()
