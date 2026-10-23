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
  subagent_preamble  cache_creation on is_sidechain=1 turns (the inherited preamble).
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
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing
from scanner import scan

MODEL = "claude-sonnet-4-6"
T0 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
CAUSES = ("resident_output", "repeated_reads", "subagent_preamble", "idle_gap", "uncached_input")
KEYS = {"cause", "session_id", "usd", "detail", "fix", "tier", "advice", "evidence", "session_usd"}


def _ts(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(sid, n, ts, inp=200, read=0, create=400, out=300, tools=(), side=False):
    """One assistant record in the Claude Code envelope the scanner parses."""
    content = [{"type": "text", "text": "..."}] + [
        {"type": "tool_use", "id": f"tu-{sid}-{n}-{i}", "name": name, "input": arg}
        for i, (name, arg) in enumerate(tools)]
    return json.dumps({
        "type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/home/dev/acme/app",
        "isSidechain": side, "version": "2.1.220",
        "message": {"id": f"msg-{sid}-{n}", "model": MODEL, "content": content,
                    "stop_reason": "tool_use" if tools else "end_turn",
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": create}},
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
EXPECTED_TOP = {"resident": "resident_output", "reread": "repeated_reads",
                "subagent": "subagent_preamble", "idle": "idle_gap",
                "uncached": "uncached_input", "mixed": "repeated_reads"}


class _Scanned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        cls.waste = importlib.import_module("waste")  # ModuleNotFoundError = loud red

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
