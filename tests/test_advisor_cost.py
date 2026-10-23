"""test_advisor_cost.py - the cost path for advisor iterations and 1h cache writes.

Advisor iterations and 1h cache writes are priced here; reading only the top
level undercounts them.

  1. An ``advisor`` tool call runs as an extra ``usage.iterations[]`` entry of type
     'advisor_message', on the stronger model named by the record's ``advisorModel``.
     The envelope's top-level ``input_tokens``/``output_tokens`` count ONLY the
     'message' iterations, so every advisor inference was invisible.
  2. Cache writes with a 1-hour TTL bill at 2x input, not the 5-minute 1.25x.

This module is a falsification harness, not just a regression test:
``test_falsify_*`` deliberately breaks one rate at a time and asserts the check goes
red. A cost test that cannot be made to fail proves nothing.
"""

import json
import unittest
from pathlib import Path

import os
import shutil
import sqlite3
import tempfile

import pricing
import sources
import sources.claude
import scanner
from scanner import parse_jsonl_file

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude" / "golden.jsonl"

# The golden record's known token counts (msg_advisor_iter_001). Hand-priced below.
PARENT_MODEL, ADVISOR_MODEL = "claude-opus-5", "claude-fable-5"
P_IN, P_OUT, P_CR = 4, 500, 200_000
P_CC, P_CC_5M, P_CC_1H = 100_000, 40_000, 60_000
A_IN, A_OUT = 300_000, 20_000

# Hand arithmetic, spelled out rather than recomputed from the table, so that a
# wrong rate in pricing.py cannot quietly agree with a wrong expectation here.
#   parent  in  4       x  $5.00/MTok = $0.00002
#           out 500     x $25.00/MTok = $0.0125
#           cr  200,000 x  $0.50/MTok = $0.10
#           cc   40,000 x  $6.25/MTok = $0.25     (5m TTL, 1.25x input)
#           cc   60,000 x $10.00/MTok = $0.60     (1h TTL, 2x input)
#   advisor in  300,000 x $10.00/MTok = $3.00     (fable-5, not opus-5)
#           out  20,000 x $50.00/MTok = $1.00
EXPECTED_PARENT = 0.00002 + 0.0125 + 0.10 + 0.25 + 0.60
EXPECTED_ADVISOR = 3.00 + 1.00
EXPECTED_TOTAL = EXPECTED_PARENT + EXPECTED_ADVISOR  # $4.96252


# Minimal turn row for the aggregate_sessions tests below.
TURN_STUB = {
    "session_id": "s1", "timestamp": "2026-08-17T10:00:00Z", "model": "",
    "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
    "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
    "cache_creation_1h_tokens": 0, "tool_name": None, "cwd": "/tmp",
    "message_id": "", "duration_ms": 0, "stop_reason": "", "service_tier": "",
    "inference_geo": "", "is_sidechain": 0, "is_compact_summary": 0,
    "tool_calls": None, "agent_id": None, "effort": "", "agent": "claude",
}


def _turns():
    """Parse the golden corpus and return the advisor record's turns by message_id."""
    _metas, turns, _n = parse_jsonl_file(str(FIXTURE))
    return {t["message_id"]: t for t in turns}


def _cost(turn):
    return pricing.calc_cost(
        turn["model"], turn["input_tokens"], turn["output_tokens"],
        turn["cache_read_tokens"], turn["cache_creation_tokens"],
        cache_creation_1h=turn["cache_creation_1h_tokens"],
    )


class TestAdvisorTurnExtraction(unittest.TestCase):
    def test_advisor_iteration_becomes_its_own_turn(self):
        turns = _turns()
        self.assertIn("msg_advisor_iter_001", turns)
        self.assertIn("advisor:msg_advisor_iter_001:1", turns)

    def test_advisor_turn_is_priced_on_the_advisor_model(self):
        """The whole point: advisor tokens bill at fable-5, not the parent's opus-5.
        Pricing them as the parent model would undercount by half."""
        adv = _turns()["advisor:msg_advisor_iter_001:1"]
        self.assertEqual(adv["model"], ADVISOR_MODEL)
        self.assertEqual(adv["input_tokens"], A_IN)
        self.assertEqual(adv["output_tokens"], A_OUT)

    def test_parent_turn_keeps_only_its_own_tokens(self):
        """No double-count: the parent must still carry the envelope's top-level
        numbers, which already exclude the advisor iteration."""
        p = _turns()["msg_advisor_iter_001"]
        self.assertEqual(
            (p["input_tokens"], p["output_tokens"], p["cache_read_tokens"],
             p["cache_creation_tokens"], p["cache_creation_1h_tokens"]),
            (P_IN, P_OUT, P_CR, P_CC, P_CC_1H))

    def test_advisor_turn_inherits_effort_and_sidechain(self):
        """Advisor cost is induced by the parent's effort level and shares its
        main-chain/subagent status — otherwise a Cost-by-Effort view splits one
        turn's spend across 'high' and 'unknown', reviving the original bug."""
        turns = _turns()
        parent, adv = turns["msg_advisor_iter_001"], turns["advisor:msg_advisor_iter_001:1"]
        self.assertEqual(adv["effort"], parent["effort"])
        self.assertEqual(adv["effort"], "high")
        self.assertEqual(adv["is_sidechain"], parent["is_sidechain"])

    def test_rescan_is_idempotent(self):
        """Synthetic ids must be stable, or a re-scan double-bills the advisor."""
        self.assertEqual(sorted(_turns()), sorted(_turns()))


class TestAdvisorDoesNotRelabelSession(unittest.TestCase):
    """Regression: fable-5 outranks opus in MODEL_PRIORITY, so before this guard a
    single advisor call relabeled the whole session as claude-fable-5 (it hit 214
    real sessions). Advisor turns must not vote for the primary model."""

    def test_helper_identifies_advisor_turns(self):
        self.assertTrue(scanner.is_advisor_turn({"message_id": "advisor:msg_x:0"}))
        self.assertFalse(scanner.is_advisor_turn({"message_id": "msg_x"}))
        self.assertFalse(scanner.is_advisor_turn({"message_id": ""}))
        self.assertFalse(scanner.is_advisor_turn({}))

    def test_advisor_turn_does_not_win_primary_model(self):
        """One opus parent + THREE fable-5 advisor turns: the session stays opus.

        The advisor turns must OUTNUMBER the parent, or `most_common` breaks the
        tie in the parent's favour by insertion order and the test passes whether
        or not the guard exists (verified: with 1-vs-1 it stays green with the
        guard removed).
        """
        metas = [{"session_id": "s1", "project_name": "p", "first_timestamp": "t",
                  "last_timestamp": "t", "git_branch": "", "topic": None,
                  "topic_source": None}]
        turns = [dict(TURN_STUB, session_id="s1", model=PARENT_MODEL,
                      message_id="msg_x", input_tokens=10, output_tokens=10)]
        turns += [dict(TURN_STUB, session_id="s1", model=ADVISOR_MODEL,
                       message_id="advisor:msg_x:%d" % i,
                       input_tokens=99, output_tokens=99) for i in range(3)]
        out = scanner.aggregate_sessions(metas, turns)
        self.assertEqual(out[0]["model"], PARENT_MODEL)

    def test_reconcile_pass_also_excludes_advisor_turns(self):
        """aggregate_sessions is only the per-file path; reconcile_session_models is
        authoritative and runs over SQL, so it needs the same exclusion."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        scanner.init_db(conn)
        conn.execute("INSERT INTO sessions (session_id, model) VALUES ('s1', 'x')")
        for mid, model in (("msg_x", PARENT_MODEL), ("advisor:msg_x:1", ADVISOR_MODEL)):
            conn.execute(
                "INSERT INTO turns (session_id, model, message_id, is_sidechain) "
                "VALUES ('s1', ?, ?, 0)", (model, mid))
        scanner.reconcile_session_models(conn)
        got = conn.execute("SELECT model FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual(got["model"], PARENT_MODEL)


class TestCostMatchesHandArithmetic(unittest.TestCase):
    def test_parent_turn_cost(self):
        self.assertAlmostEqual(_cost(_turns()["msg_advisor_iter_001"]),
                               EXPECTED_PARENT, places=6)

    def test_advisor_turn_cost(self):
        self.assertAlmostEqual(_cost(_turns()["advisor:msg_advisor_iter_001:1"]),
                               EXPECTED_ADVISOR, places=6)

    def test_total_cost(self):
        turns = _turns()
        total = sum(_cost(t) for k, t in turns.items() if "advisor_iter_001" in k)
        self.assertAlmostEqual(total, EXPECTED_TOTAL, places=6)

    def test_1h_split_costs_more_than_5m(self):
        """Directional guard: the whole 1h fix is that it is strictly pricier."""
        as_5m = pricing.calc_cost(PARENT_MODEL, 0, 0, 0, P_CC, cache_creation_1h=0)
        as_1h = pricing.calc_cost(PARENT_MODEL, 0, 0, 0, P_CC, cache_creation_1h=P_CC)
        self.assertGreater(as_1h, as_5m)

    def test_1h_portion_cannot_exceed_total(self):
        """Old rows have cc but no split; a bogus 1h value must not inflate cost."""
        clamped = pricing.calc_cost(PARENT_MODEL, 0, 0, 0, 1000, cache_creation_1h=9999)
        allcc = pricing.calc_cost(PARENT_MODEL, 0, 0, 0, 1000, cache_creation_1h=1000)
        self.assertAlmostEqual(clamped, allcc, places=9)

    def test_missing_1h_rate_falls_back_to_5m(self):
        """OpenAI entries have no 1h tier; they must not KeyError or price at 0."""
        codex = pricing.calc_cost("gpt-5.6-sol", 0, 0, 0, 1_000_000,
                                  cache_creation_1h=1_000_000)
        self.assertAlmostEqual(codex, pricing.PRICING["gpt-5.6-sol"]["cache_write"],
                               places=6)


class TestFalsification(unittest.TestCase):
    """Break one rate at a time; each must turn the cost check red.

    A green suite only means something if these pass — they are the evidence that
    the assertions above are actually reading the rates they claim to read.
    """

    def setUp(self):
        self._saved = {k: dict(v) for k, v in pricing.PRICING.items()
                       if isinstance(v, dict)}

    def tearDown(self):
        for k, v in self._saved.items():
            pricing.PRICING[k] = v

    def _assert_total_now_wrong(self, msg):
        total = sum(_cost(t) for k, t in _turns().items() if "advisor_iter_001" in k)
        self.assertNotAlmostEqual(total, EXPECTED_TOTAL, places=6, msg=msg)

    def test_falsify_advisor_input_rate(self):
        pricing.PRICING[ADVISOR_MODEL]["input"] = 1.00
        self._assert_total_now_wrong("advisor input rate is not reaching the total")

    def test_falsify_advisor_output_rate(self):
        pricing.PRICING[ADVISOR_MODEL]["output"] = 1.00
        self._assert_total_now_wrong("advisor output rate is not reaching the total")

    def test_falsify_1h_cache_write_rate(self):
        pricing.PRICING[PARENT_MODEL]["cache_write_1h"] = 6.25
        self._assert_total_now_wrong("1h cache-write rate is not reaching the total")

    def test_falsify_5m_cache_write_rate(self):
        pricing.PRICING[PARENT_MODEL]["cache_write"] = 99.0
        self._assert_total_now_wrong("5m cache-write rate is not reaching the total")

    def test_falsify_parent_output_rate(self):
        pricing.PRICING[PARENT_MODEL]["output"] = 1.00
        self._assert_total_now_wrong("parent output rate is not reaching the total")

    def test_falsify_cache_read_rate(self):
        pricing.PRICING[PARENT_MODEL]["cache_read"] = 99.0
        self._assert_total_now_wrong("cache_read rate is not reaching the total")

    def test_falsify_dropping_the_advisor_turn(self):
        """The original bug, reproduced: price the parent alone and the total must
        come out short. If this passes, the advisor turn was never counted."""
        parent_only = _cost(_turns()["msg_advisor_iter_001"])
        self.assertLess(parent_only, EXPECTED_TOTAL)
        self.assertAlmostEqual(EXPECTED_TOTAL - parent_only, EXPECTED_ADVISOR, places=6)

    def test_falsify_pricing_advisor_at_parent_model(self):
        """The tempting wrong fix — fold advisor tokens into the parent turn —
        would halve the advisor's cost. Confirm the two models really differ."""
        as_parent = pricing.calc_cost(PARENT_MODEL, A_IN, A_OUT, 0, 0)
        as_advisor = pricing.calc_cost(ADVISOR_MODEL, A_IN, A_OUT, 0, 0)
        self.assertNotAlmostEqual(as_parent, as_advisor, places=6)


class TestUpgradeBackfill(unittest.TestCase):
    """An existing DB must gain its advisor turns on the next scan.

    The incremental walk skips files whose mtime is unchanged, and turn inserts
    are INSERT OR IGNORE, so neither a plain rescan nor an in-place UPDATE could
    add advisor rows: an existing DB would stay undercounted forever.
    `force_reparse_once` clears the file cache exactly once to solve that.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects", "proj")
        os.makedirs(self.projects)
        shutil.copy(FIXTURE, os.path.join(self.projects, "sess.jsonl"))
        self.db = os.path.join(self.tmp, "usage.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self):
        return [sources.claude.ClaudeSource(
            projects_dirs=[os.path.join(self.tmp, "projects")])]

    def _advisor_rows(self):
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM turns "
                         "WHERE message_id LIKE 'advisor:%'").fetchone()[0]
        conn.close()
        return n

    def test_pre_fix_db_gains_advisor_turns_on_next_scan(self):
        # Build a DB, then simulate a pre-fix one: drop the advisor rows but LEAVE
        # processed_files intact (the file is unchanged, so a plain scan skips it)
        # and clear the marker so the one-time re-parse is pending again.
        sources.scan_all(db_path=self.db, verbose=False, sources=self._src())
        self.assertGreater(self._advisor_rows(), 0, "fixture should yield advisor rows")

        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM turns WHERE message_id LIKE 'advisor:%'")
        conn.execute("DELETE FROM schema_meta WHERE key = 'advisor_reparse_done_claude'")
        conn.commit(); conn.close()
        self.assertEqual(self._advisor_rows(), 0)

        sources.scan_all(db_path=self.db, verbose=False, sources=self._src())
        self.assertGreater(self._advisor_rows(), 0,
                           "upgrade backfill did not restore advisor turns")

    def test_repeated_scans_do_not_duplicate_advisor_turns(self):
        """INSERT OR IGNORE + the unique message_id index must hold under a forced
        re-parse, or the backfill would double-bill instead of fixing anything."""
        sources.scan_all(db_path=self.db, verbose=False, sources=self._src())
        first = self._advisor_rows()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM processed_files")  # force a full re-parse
        conn.commit(); conn.close()
        sources.scan_all(db_path=self.db, verbose=False, sources=self._src())
        self.assertEqual(self._advisor_rows(), first)

    def test_backfill_runs_only_once(self):
        sources.scan_all(db_path=self.db, verbose=False, sources=self._src())
        conn = sqlite3.connect(self.db)
        marker = conn.execute(
            "SELECT value FROM schema_meta WHERE key='advisor_reparse_done_claude'"
        ).fetchone()
        conn.close()
        self.assertEqual(marker[0], "1")
        # Second call must clear nothing (marker already set).
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        self.assertEqual(scanner.force_reparse_once(
            conn, "advisor_reparse_done_claude", "claude", verbose=False), 0)
        conn.close()


class TestIterationTypeCanary(unittest.TestCase):
    def test_unknown_iteration_type_is_flagged(self):
        """A third iteration type would be another unrecorded billable class."""
        import schema
        rec = {"type": "assistant", "message": {"usage": {
            "iterations": [{"type": "message"}, {"type": "reviewer_message"}]}}}
        self.assertEqual(schema.inspect_record(rec)["unknown_iteration_types"],
                         ["reviewer_message"])
        self.assertFalse(schema.record_is_known(rec))

    def test_known_iteration_types_are_clean(self):
        import schema
        rec = {"type": "assistant", "message": {"usage": {
            "iterations": [{"type": "message"}, {"type": "advisor_message"}]}}}
        self.assertEqual(schema.inspect_record(rec)["unknown_iteration_types"], [])


if __name__ == "__main__":
    unittest.main()
