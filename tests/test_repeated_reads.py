"""repeated_reads keys on (path, offset, limit), forgets a path on write and on compaction,
and caps its claim by the repeated Reads' result size."""
import shutil
import tempfile
import unittest
from pathlib import Path

import policy
import waste
from scanner import get_db
from sources import scan_all
from sources.claude import ClaudeSource
from tests.test_waste import _main_chain, _rec, _ts


def read(offset=None, limit=None, path="/src/big.py"):
    return ("Read", {"file_path": path, **({"offset": offset} if offset is not None else {}), **({"limit": limit} if limit is not None else {})})


G = {t: 30_000 for t in range(2, 11)}
SESSIONS = {
    "ranged": _main_chain("ranged", 10, 20_000, tool_at={t: [read(1 + 50 * t, 50)] for t in (2, 4, 6, 8, 10)}, growth=G),
    "same_range": _main_chain("same_range", 10, 20_000, tool_at={t: [read(1, 50)] for t in (2, 4, 6, 8, 10)}, growth=G),
    "edited": _main_chain("edited", 10, 20_000, tool_at={2: [read()], 3: [("Edit", {"file_path": "/src/big.py"})], 4: [read()], 5: [("Write", {"file_path": "/src/big.py"})], 6: [read()], 7: [("Edit", {"file_path": "/src/big.py"})], 8: [read()], 9: [("Edit", {"file_path": "/src/big.py"})], 10: [read()]}, growth=G),
    "whole_twice": _main_chain("whole_twice", 10, 20_000, tool_at={t: [read()] for t in (2, 4, 6, 8, 10)}, growth=G),
    "compacted": [
        _rec("compacted", 1, _ts(0), read=0, create=20_000, tools=[read()], result_tokens=30_400),
        _rec("compacted", 2, _ts(1), read=20_000, create=5_000, compact=True, result_tokens=None),
        _rec("compacted", 3, _ts(2), read=25_000, create=400, tools=[read()], result_tokens=30_400),
        _rec("compacted", 4, _ts(3), read=25_400, create=30_000),
    ],
    # the compaction summary turn itself reads the file: that read is in context afterwards
    "compact_reads": [
        _rec("compact_reads", 1, _ts(0), read=0, create=20_000),
        _rec("compact_reads", 2, _ts(1), read=20_000, create=5_000, compact=True, tools=[read()], result_tokens=30_400),
        _rec("compact_reads", 3, _ts(2), read=25_000, create=400, tools=[read()], result_tokens=30_400),
        _rec("compact_reads", 4, _ts(3), read=25_400, create=30_000),
    ],
}


class TestRepeatedReads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in SESSIONS.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        scan_all(db_path=cls.db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(proj.parent)])])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def causes(self, sid):
        return {f["cause"] for f in waste.report(self.db, session_id=sid)}

    def test_a_ranged_re_read_is_not_a_repeat(self):
        self.assertNotIn("repeated_reads", self.causes("ranged"))

    def test_the_same_range_twice_is_a_repeat(self):
        self.assertIn("repeated_reads", self.causes("same_range"))
        self.assertIn("repeated_reads", self.causes("whole_twice"))

    def test_a_re_read_after_an_edit_is_not_a_repeat(self):
        self.assertNotIn("repeated_reads", self.causes("edited"))

    def test_a_re_read_after_compaction_is_not_a_repeat(self):
        self.assertNotIn("repeated_reads", self.causes("compacted"))

    def test_a_read_in_the_compaction_turn_itself_counts_as_in_context(self):
        self.assertIn("repeated_reads", self.causes("compact_reads"))

    def test_offset_and_limit_are_stored_as_numbers_only(self):
        conn = get_db(self.db)
        try:
            row = conn.execute("SELECT tool_calls FROM turns WHERE session_id='ranged' AND tool_calls LIKE '%offset%' LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIn('"offset": 1', row[0])
        self.assertIn('"limit": 50', row[0])


class TestPolicy(unittest.TestCase):
    def test_only_integers_survive(self):
        out = policy.redact_call({"name": "Read", "input": {"file_path": "/a", "offset": 5, "limit": True}})["input"]
        self.assertEqual((out["offset"], "limit" in out), (5, False))
        self.assertNotIn("offset", policy.redact_call({"name": "Read", "input": {"offset": "5"}})["input"])
        self.assertNotIn("offset", policy.redact_call({"name": "Grep", "input": {"offset": 5}})["input"])


class TestClaimCap(unittest.TestCase):
    def claim(self, sizes, rep_idx, creation=30_000, results=None):
        """sizes: estimated result tokens per call of the previous turn (None = unknown)."""
        claims = {c: [0.0, [], []] for c in waste.CAUSES}
        turn = {"model": "claude-opus-5-5", "timestamp": "2026-11-01T10:00:00Z", "mult": 1.0, "cache_creation_1h_tokens": 20_000, "cache_creation_tokens": 30_000,
                "message_id": "m", "agent_id": None}
        unknown = [0]
        got = waste._repeat_claim(claims, turn, creation, {"aligned": sizes, "names": ["Read"] * len(sizes), "rep_idx": rep_idx,
                                                          "repeated": ["/a"] * len(rep_idx),
                                                          "results": results or ([("Read", n) for n in sizes if n is not None], sum(n is None for n in sizes))}, unknown)
        tok = lambda cause: claims[cause][2][0]["tokens"] if claims[cause][2] else 0
        self.resident = tok("resident_output")
        return got, tok("repeated_reads"), unknown[0]

    def test_cap_is_the_repeated_calls_own_result(self):
        self.assertEqual(self.claim([8_000, 8_000, 8_000, 8_000], [1]), (True, 8_000, 0))

    def test_split_claims_never_cost_more_than_the_write(self):
        claims = {c: [0.0, [], []] for c in waste.CAUSES}
        turn = {"model": "claude-opus-5-5", "timestamp": "2026-11-01T10:00:00Z", "mult": 1.0, "cache_creation_1h_tokens": 15_000,
                "cache_creation_tokens": 30_000, "message_id": "m", "agent_id": None}
        waste._repeat_claim(claims, turn, 30_000, {"aligned": [15_000, 15_000], "names": ["Read", "Bash"], "rep_idx": [0], "repeated": ["/a"],
                                                   "results": ([("Read", 15_000), ("Bash", 15_000)], 0)}, [0])
        self.assertLessEqual(claims["repeated_reads"][0] + claims["resident_output"][0], waste._cost(turn, creation=30_000) + 1e-9)
        self.assertGreater(claims["resident_output"][0], 0)

    def test_the_rest_of_the_write_goes_to_the_batchs_other_results(self):
        self.claim([100, 29_900], [0])
        self.assertEqual(self.resident, 29_900)   # 30,000 write: 100 repeated, the rest resident, claims disjoint

    def test_a_streamed_message_keeps_its_earlier_results_for_the_remainder(self):
        # the turn row kept only its last call (the repeated Read); the earlier Bash result is still a known result
        self.claim([100], [0], results=([("Bash", 29_900), ("Read", 100)], 0))
        self.assertEqual(self.resident, 29_900)

    def test_a_small_repeat_beside_a_big_fresh_read_claims_the_small_one(self):
        self.assertEqual(self.claim([100, 59_900], [0]), (True, 100, 0))

    def test_never_above_the_write(self):
        self.assertEqual(self.claim([90_000], [0]), (True, 30_000, 0))

    def test_an_unknown_repeat_claims_nothing_and_is_counted(self):
        self.assertEqual(self.claim([None, 50_000], [0]), (False, 0, 0))   # nothing claimed: caller falls back to resident
        self.assertEqual(self.claim([100, None], [0, 1]), (True, 100, 1))  # known part claimed, unknown part counted

    def test_a_zero_sized_repeat_does_not_block_resident_attribution(self):
        self.assertEqual(self.claim([0, 30_000], [0]), (False, 0, 0))

    def test_results_join_by_tool_use_id_whatever_order_they_arrived_in(self):
        import json
        row = {"message_id": "m", "tool_calls": json.dumps([{"name": "Read", "id": "a"}, {"name": "Read", "id": "b"}]), "tool_name": "Read"}
        b = waste.RESULT_BYTES_PER_TOKEN
        # b's result was written first: positional order would swap the two sizes
        got = waste._aligned(row, {"m": [("Read", 59_900 * b), ("Read", 100 * b)]}, {"b": 59_900 * b, "a": 100 * b})
        self.assertEqual(got, [100.0, 59_900.0])
        self.assertEqual(waste._aligned(row, {}, {"a": 100 * b}), [100.0, None])   # no row for b: unknown, not zero

    def test_results_line_up_with_calls_by_name_or_are_unknown(self):
        call = lambda n: {"name": n, "input": {}}
        row = {"message_id": "m", "tool_calls": __import__("json").dumps([call("Read"), call("Bash")]), "tool_name": "Read"}
        b = waste.RESULT_BYTES_PER_TOKEN
        self.assertEqual(waste._aligned(row, {"m": [("Read", 10 * b), ("Bash", None)]}), [10.0, None])
        self.assertEqual(waste._aligned(row, {"m": [("Bash", 10 * b), ("Read", 5)]}), [None, None])
        self.assertEqual(waste._aligned(row, {"m": [("Read", 10 * b)]}), [None, None])


if __name__ == "__main__":
    unittest.main()
