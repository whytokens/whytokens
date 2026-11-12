"""next_step_action: the one rule, ranked calls and lower-bound estimate on a resident_output finding."""
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import waste
from sources import ClaudeSource, scan_all
from tests.test_waste import _main_chain, _rec, _ts, RES

READS = {t: [("Read", {"file_path": rf"C:\Users\someone\src\f{t}.py"})] for t in (2, 4, 6)}
BASH = {t: [("Bash", {"command": "ls"})] for t in (3, 5, 7, 8, 9)}
BASH[9] = BASH[9] + [("Bash", {"command": "pwd"})]  # one call has no result record: unknown, never zero
TWO = {10: [("Read", {"file_path": "/a/x.py"}), ("Read", {"file_path": "/a/y.py"})]}  # ambiguous: no basename
FIXTURES = {
    # three 40K-token Reads among many small Bash calls
    "reads": _main_chain("reads", 12, base=20_000, tool_at={**READS, **BASH, **TWO, 12: [("Read", {"file_path": "/z/last.py"})]},
                         growth={2: 40_000, 4: 40_000, 6: 40_000}, results={3: 300, 5: 300, 7: 300, 8: 300, 9: [300, None], 10: [8_000, 7_000], 12: 1_000_000}),
    # only small results (and a big resident_output finding is not needed: the action must be null)
    "small": _main_chain("small", 12, base=20_000, tool_at={t: [("Bash", {"command": "ls"})] for t in range(2, 9)},
                         growth={t: 3_000 for t in range(2, 9)}),
}


def _lanes(sid, turns_per_lane):
    """Main dispatches, then one subagent lane per entry: a cold 30k preamble and n-1 warm turns."""
    out, n = [_rec(sid, 1, _ts(0), read=0, create=10_000, tools=[("Agent", {"prompt": "a"})], result_tokens=RES)], 1
    for lane, count in enumerate(turns_per_lane):
        for k in range(count):
            n += 1
            out.append(_rec(sid, n, _ts(1 + lane * 20 + k), read=0 if k == 0 else 30_000, create=30_000 if k == 0 else 100,
                            side=True, agent=f"a{lane}"))
    return out


FIXTURES.update({
    "main_gap": _main_chain("main_gap", 10, base=60_000, gap_after=5),
    "lane_gap": [_rec("lane_gap", 1, _ts(0), read=0, create=10_000, tools=[("Agent", {"prompt": "a"})]),
                 _rec("lane_gap", 2, _ts(1), read=0, create=30_000, side=True, agent="a1"),
                 _rec("lane_gap", 3, _ts(2), read=30_000, create=500, side=True, agent="a1",
                      tools=[("Bash", {"command": "sleep"})], result_tokens=RES),
                 _rec("lane_gap", 4, _ts(10), read=0, create=31_000, side=True, agent="a1"),
                 _rec("lane_gap", 5, _ts(11), read=10_000, create=400)],
    "small_lanes": _lanes("small_lanes", [3, 3, 12]),   # two short lanes, one long
    "big_lanes": _lanes("big_lanes", [12, 15]),         # none short: action null
})


class TestNextStepAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        scan_all(db_path=cls.db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(proj.parent)])])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _resident(self, sid):
        return [f for f in waste.report(self.db, session_id=sid, min_usd=0, min_share=0)
                if f["cause"] == "resident_output"]

    def test_three_big_reads_give_read_ranges_ranked_with_basenames(self):
        (f,) = self._resident("reads")
        a = f["action"]
        self.assertEqual(a["id"], "read_ranges")
        sizes = [c["result_tokens"] for c in a["calls"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual({c["tool"] for c in a["calls"]}, {"Read"})
        self.assertEqual(len(a["calls"]), 5)
        self.assertEqual(sizes[3:], [8_000, 7_000])
        self.assertEqual(sorted(c["file"] for c in a["calls"][:3]), ["f2.py", "f4.py", "f6.py"])
        self.assertEqual([c["file"] for c in a["calls"][3:]], [None, None])  # two Reads in one turn: not matched
        self.assertTrue(all(c["prompt_seq"] is not None for c in a["calls"]))
        self.assertGreater(a["est_usd"], 0)
        self.assertIn("lower bound", a["est_basis"])
        self.assertIn("offset", a["rule"])

    def test_result_of_the_last_turn_is_never_counted(self):
        self.assertTrue(all(c["result_tokens"] < 1_000_000 for f in self._resident("reads") for c in f["action"]["calls"]))

    def test_only_small_results_give_null(self):
        for f in self._resident("small"):
            self.assertIsNone(f["action"])

    def test_non_resident_findings_carry_a_null_action(self):
        self.assertTrue(all(f["action"] is None for f in waste.report(self.db, min_usd=0, min_share=0)
                            if f["cause"] not in ("resident_output", "idle_gap", "subagent_preamble")))

    def _find(self, sid, cause):
        return [f for f in waste.report(self.db, session_id=sid, min_usd=0, min_share=0) if f["cause"] == cause]

    def test_main_thread_gap_gives_compact_before_break_with_upper_bound(self):
        (f,) = self._find("main_gap", "idle_gap")
        a = f["action"]
        self.assertEqual(a["id"], "compact_before_break")
        self.assertEqual(len(a["calls"]), 1)
        self.assertEqual(a["calls"][0]["minutes"], 45)
        self.assertAlmostEqual(a["est_usd"], a["calls"][0]["usd"] - a["calls"][0]["warm_usd"])
        self.assertGreater(a["est_usd"], 0)
        self.assertIn("upper bound", a["est_basis"])

    def test_subagent_lane_gap_gives_subagent_waits(self):
        (f,) = self._find("lane_gap", "idle_gap")
        self.assertEqual(f["action"]["id"], "subagent_waits")
        self.assertEqual(f["action"]["calls"][0]["minutes"], 8)
        self.assertIn("agent_type", f["action"]["calls"][0])
        ev = [{"message_id": "m", "at": "t", "usd": 2.0, "agent_id": "a1"}]
        got = waste.idle_gap_action(ev, [(timedelta(minutes=9), 5, 0.5)], {"a1": "Explore"})
        self.assertEqual(got["calls"][0]["agent_type"], "Explore")

    def test_short_subagent_lanes_give_small_jobs_inline(self):
        (f,) = self._find("small_lanes", "subagent_preamble")
        a = f["action"]
        self.assertEqual(a["id"], "small_jobs_inline")
        self.assertEqual(sorted(c["turns"] for c in a["calls"]), [3, 3])
        self.assertIn("2 of 3", a["title"])
        self.assertAlmostEqual(a["est_usd"], sum(c["usd"] for c in a["calls"]))
        self.assertIn("upper bound", a["est_basis"])

    def test_no_short_lane_gives_null_action(self):
        (f,) = self._find("big_lanes", "subagent_preamble")
        self.assertIsNone(f["action"])


class TestNoGapOrPreambleLeak(unittest.TestCase):
    def test_title_counts_lanes_not_evidence_records(self):
        ev = [{"message_id": "m", "at": "t", "usd": 1.0, "agent_id": "a"}] +              [{"message_id": "m", "at": "t", "usd": 1.0, "agent_id": None}] * 3
        t = waste.small_jobs_action(ev, {"a": 2}, {"a": "Explore"})["title"]
        self.assertIn("1 of 1 subagents", t)
        self.assertIn("3 unidentified", t)
        self.assertEqual(waste.small_jobs_action(ev, {"a": 2}, {"a": "Explore"})["calls"][0]["agent_type"], "Explore")

    def test_lane_without_id_is_never_small(self):
        ev = [{"message_id": "m", "at": "t", "usd": 1.0, "agent_id": None}]
        self.assertIsNone(waste.small_jobs_action(ev, {None: 1, "a": 1}, {}))


def _call(tool, tokens, file=None):
    return {"message_id": "m", "at": "t", "tool": tool, "file": file, "result_tokens": tokens,
            "prompt_seq": 1, "excess_usd": 0.01}


class TestRulePick(unittest.TestCase):
    def test_each_tool_maps_to_its_rule(self):
        for tool, rid in (("Read", "read_ranges"), ("Bash", "bash_tail"), ("Grep", "search_limits"),
                          ("Glob", "search_limits"), ("mcp__srv__shot", "mcp_text")):
            self.assertEqual(waste.next_step_action([_call(tool, 9_000)])["id"], rid, tool)

    def test_largest_oversized_total_wins_not_call_count(self):
        calls = [_call("Bash", 5_000)] * 4 + [_call("Read", 30_000)]
        self.assertEqual(waste.next_step_action(calls)["id"], "read_ranges")

    def test_mcp_rule_names_the_server_and_unmapped_or_small_is_null(self):
        self.assertIn("`srv`", waste.next_step_action([_call("mcp__srv__x", 9_000)])["rule"])
        self.assertIsNone(waste.next_step_action([_call("WebFetch", 90_000), _call("Read", 4_999)]))

    def test_top_five_only(self):
        got = waste.next_step_action([_call("Read", 6_000 + i) for i in range(8)])["calls"]
        self.assertEqual([c["result_tokens"] for c in got], [6_007, 6_006, 6_005, 6_004, 6_003])


if __name__ == "__main__":
    unittest.main()
