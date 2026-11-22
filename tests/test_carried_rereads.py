"""Carried re-reads: the carry window (first read two calls after an item, stopping before the next
compaction call), the call-to-result join, the per-lane split, the cache-read cap and the coverage
exclusion. scripts/falsify_carried_rereads.py breaks each and checks this file goes red."""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import pricing
import waste
from scanner import get_db, init_db

MODEL, TS = "claude-opus-5-5", "2026-11-01T00:00:00Z"
RATE = pricing.calc_cost(MODEL, 0, 0, 1, 0, TS)  # $ per cache-read token
BIG = 10_000_000  # cache reads large enough that the cap never binds


def _turn(mid, out=0, cr=BIG, compact=0, calls=None):
    return {"model": MODEL, "timestamp": TS, "mult": 1, "output_tokens": out, "cache_read_tokens": cr,
            "is_compact_summary": compact, "message_id": mid, "tool_calls": json.dumps(calls) if calls else None}


class TestCarry(unittest.TestCase):
    def test_first_read_is_two_calls_later(self):
        # a's output and result are written by b and re-read by c, d, e
        turns = [_turn("a", out=10), _turn("b"), _turn("c"), _turn("d"), _turn("e")]
        out = waste.carry(turns, {"a": [("c1", "Bash", 350)]})  # 350 bytes = 100 tokens
        self.assertAlmostEqual(out[waste.OWN_OUTPUT], 10 * 3 * RATE)
        self.assertAlmostEqual(out["Bash"], 100 * 3 * RATE)

    def test_compaction_call_does_not_reread_old_context(self):
        # d answers the compaction summary: it and later calls no longer carry a
        turns = [_turn("a", out=10), _turn("b"), _turn("c"), _turn("d", compact=1), _turn("e"), _turn("f")]
        out = waste.carry(turns, {})
        # a: read by c only; b: nothing (d is its cutoff); d: read by f
        self.assertAlmostEqual(out[waste.OWN_OUTPUT], 10 * 1 * RATE)
        turns[3]["output_tokens"] = 7
        self.assertAlmostEqual(waste.carry(turns, {})[waste.OWN_OUTPUT], (10 + 7) * RATE)

    def test_read_results_join_their_call_for_the_path(self):
        calls = [{"id": "c1", "name": "Read", "input": {"file_path": "/x/references/a.md"}},
                 {"id": "c2", "name": "Read", "input": {"file_path": "/y/b.xml"}}]
        turns = [_turn("a", calls=calls), _turn("b"), _turn("c")]
        results = {"a": [("c1", "Read", 350), ("c2", "Read", 700), ("c9", "Read", 35)]}
        out = waste.carry(turns, results, [("refs", re.compile("references"))])
        self.assertAlmostEqual(out["Read:refs"], 100 * RATE)
        self.assertAlmostEqual(out["Read:other"], 200 * RATE)
        self.assertAlmostEqual(out[waste.READ_PATH_UNKNOWN], 10 * RATE)  # call id not on the turn

    def test_attribution_is_capped_at_the_calls_cache_read(self):
        turns = [_turn("a", out=1000), _turn("b"), _turn("c", cr=250)]
        out = waste.carry(turns, {"a": [("c1", "Bash", 3500)]})  # 1000 + 1000 tokens resident, 250 read
        self.assertAlmostEqual(sum(out.values()), 250 * RATE)


class TestCarriedRereadsDb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.db"
        conn = get_db(self.db)
        init_db(conn)
        rows = [  # main lane m1..m3 interleaved in time with a subagent lane a1..a3
            ("s", "m1", "", 1, 10), ("s", "a1", "ag", 2, 1000), ("s", "m2", "", 3, 0),
            ("s", "a2", "ag", 4, 0), ("s", "m3", "", 5, 0), ("s", "a3", "ag", 6, 0),
            ("gone", "g1", "", 1, 5), ("gone", "g2", "", 2, 0), ("gone", "g3", "", 3, 0)]
        for sid in ("s", "gone"):
            conn.execute("INSERT INTO sessions (session_id, agent) VALUES (?, 'claude')", (sid,))
        for sid, mid, aid, sec, out in rows:
            calls = json.dumps([{"id": "c1", "name": "Read", "input": {"file_path": "/r/x"}}]) if mid in ("m1", "g1") else None
            conn.execute("INSERT INTO turns (session_id, message_id, agent_id, model, agent, timestamp, output_tokens, "
                         "cache_read_tokens, tool_calls) VALUES (?, ?, ?, ?, 'claude', ?, ?, ?, ?)",
                         (sid, mid, aid or None, MODEL, f"2026-11-01T00:00:0{sec}Z", out, BIG, calls))
        # m1's call has a measured result; g1's call has none (stored before per-call ingestion)
        conn.execute("INSERT INTO tool_results (call_id, session_id, message_id, tool_name, result_bytes) "
                     "VALUES ('c1', 's', 'm1', 'Read', 350)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lanes_carry_separately_and_unsized_sessions_are_excluded(self):
        r = waste.carried_rereads(self.db)
        # m1 (10 out + 100 result tokens) is re-read by m3 only; a1 (1000 out) by a3 only
        self.assertAlmostEqual(r["by_source"][waste.OWN_OUTPUT], (10 + 1000) * RATE)
        self.assertAlmostEqual(r["by_source"]["Read"], 100 * RATE)
        self.assertEqual((r["sessions"], r["lanes"], r["excluded_sessions"]), (1, 2, 1))
        self.assertAlmostEqual(r["excluded_cache_read_usd"], 3 * BIG * RATE)
        self.assertFalse(r["in_total"])
        self.assertAlmostEqual(r["rest_usd"], 6 * BIG * RATE - (1010 + 100) * RATE)

    def test_context_built_before_the_window_is_carried_into_it(self):
        # only m3 and a3 fall in the window, yet they re-read m1 and a1 from before it
        r = waste.carried_rereads(self.db, since="2026-11-01T00:00:05Z")
        self.assertAlmostEqual(r["by_source"][waste.OWN_OUTPUT], (10 + 1000) * RATE)
        self.assertAlmostEqual(r["cache_read_usd"], 2 * BIG * RATE)  # totals count the window only
        # m3's re-read of m1 is before the window now: only a3's re-read of a1 is charged
        r = waste.carried_rereads(self.db, since="2026-11-01T00:00:06Z")
        self.assertAlmostEqual(r["by_source"][waste.OWN_OUTPUT], 1000 * RATE)
        self.assertAlmostEqual(r["by_source"]["Read"], 0.0)
        r = waste.carried_rereads(self.db, since="2026-11-01T00:00:07Z")
        self.assertEqual(r["by_source"], {})

    def test_subagent_call_without_agent_id_never_joins_the_main_lane(self):
        conn = get_db(self.db)
        for aid in (None, ""):
            conn.execute("UPDATE turns SET agent_id = ?, is_sidechain = 1 WHERE message_id IN ('a1', 'a2', 'a3')", (aid,))
            conn.commit()
            r = waste.carried_rereads(self.db)
            self.assertAlmostEqual(r["by_source"][waste.OWN_OUTPUT], 10 * RATE, msg=repr(aid))
            self.assertEqual(r["lanes"], 1)
            self.assertAlmostEqual(r["cache_read_usd"], 6 * BIG * RATE)  # its spend stays in the totals
        conn.close()


if __name__ == "__main__":
    unittest.main()
