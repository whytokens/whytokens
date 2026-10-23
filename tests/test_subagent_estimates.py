"""Estimates for what Claude Code's transcripts under-record (see
scanner._apply_estimates): truncated subagent output, advisor calls with no
usage, and AgentSummary side calls. Docs: docs/SCHEMA.md.

Run: python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner  # noqa: E402

T0 = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
SESSION = "sess-est-1"


def rec(mid, offset_s, content, stop=None, out=3, cr=1000, extra_usage=None,
        advisor_model=None, subagent=True):
    usage = {"input_tokens": 10, "output_tokens": out,
             "cache_read_input_tokens": cr, "cache_creation_input_tokens": 0}
    usage.update(extra_usage or {})
    r = {
        "type": "assistant", "sessionId": SESSION, "cwd": "/tmp/p",
        "timestamp": (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": {"id": mid, "model": "claude-opus-5", "role": "assistant",
                    "content": content, "stop_reason": stop, "usage": usage},
    }
    if subagent:
        r["agentId"] = "a1"
    if advisor_model:
        r["advisorModel"] = advisor_model
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "u.db"
        self.proj = self.tmp / "projects"

    def path(self, subagent=True):
        d = self.proj / "p" / SESSION / "subagents" if subagent else self.proj / "p"
        d.mkdir(parents=True, exist_ok=True)
        return d / ("agent-a1.jsonl" if subagent else SESSION + ".jsonl")

    def write(self, path, records, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def parse(self, path):
        return {t["message_id"]: t for t in scanner.parse_jsonl_file(str(path))[1]}

    def scan(self):
        scanner.scan(projects_dir=str(self.proj), db_path=self.db, verbose=False)
        conn = scanner.get_db(self.db)
        rows = conn.execute("SELECT * FROM turns ORDER BY message_id").fetchall()
        conn.close()
        return {r["message_id"]: r for r in rows}


class TestTruncatedOutput(Base):
    def test_midstream_message_estimated_from_all_its_lines(self):
        tool = {"type": "tool_use", "name": "Read", "input": {"p": "x" * 40}}
        chars = 100 + 300 + len(json.dumps(tool["input"])) + len("Read")
        p = self.path()
        self.write(p, [
            rec("m1", 0, [{"type": "thinking", "thinking": "t" * 100}]),
            rec("m1", 0, [{"type": "text", "text": "x" * 300}]),
            rec("m1", 0, [tool]),
            rec("m2", 5, [{"type": "text", "text": "y" * 4000}], stop="end_turn", out=50),
        ])
        t = self.parse(p)
        self.assertEqual(t["m1"]["output_tokens"], round(chars / 2.0))
        self.assertEqual(t["m1"]["estimated"], 1)
        self.assertEqual(t["m2"]["output_tokens"], 50)  # finalized: untouched
        self.assertFalse(t["m2"].get("estimated"))


class TestAdvisorEstimate(Base):
    BLOCK = [{"type": "server_tool_use", "name": "advisor", "input": {}}]
    ITER = {"iterations": [{"type": "advisor_message", "input_tokens": 5, "output_tokens": 7}]}

    def test_missing_iteration_mints_est_row_present_does_not(self):
        p = self.path(subagent=False)
        self.write(p, [
            rec("miss", 0, self.BLOCK, subagent=False, advisor_model="claude-fable-5"),
            rec("have", 9, self.BLOCK, subagent=False, extra_usage=self.ITER),
            # finalized records count too: an unrecorded advisor is unrecorded
            rec("done", 20, self.BLOCK, stop="end_turn", subagent=False),
        ])
        t = self.parse(p)
        est = t["advisor:miss:est"]
        self.assertEqual(est["model"], "claude-fable-5")
        self.assertEqual(est["output_tokens"], scanner.EST_ADVISOR_OUTPUT_TOKENS)
        self.assertEqual(est["input_tokens"], 10 + 1000)
        self.assertEqual(est["cache_read_tokens"], 0)
        self.assertEqual(est["estimated"], 1)
        self.assertIn("advisor:done:est", t)
        self.assertNotIn("advisor:have:est", t)
        self.assertIn("advisor:have:0", t)

    def test_iteration_on_an_earlier_line_blocks_the_estimate(self):
        p = self.path(subagent=False)
        self.write(p, [
            rec("m", 0, self.BLOCK, extra_usage=self.ITER, subagent=False),
            rec("m", 0, [{"type": "text", "text": "x"}], subagent=False),
        ])
        self.assertFalse([k for k in self.parse(p) if k.endswith(":est")])

    def test_real_iteration_arriving_later_deletes_the_estimate_row(self):
        p = self.path(subagent=False)
        self.write(p, [rec("m", 0, self.BLOCK, subagent=False)])
        os.utime(p, (1e9,) * 2)
        self.assertIn("advisor:m:est", self.scan())
        self.write(p, [rec("m", 0, self.BLOCK, extra_usage=self.ITER, subagent=False)], mode="a")
        os.utime(p, (1e9 + 10,) * 2)
        rows = self.scan()
        self.assertNotIn("advisor:m:est", rows)
        self.assertIn("advisor:m:0", rows)


def user(offset_s):
    return {"type": "user", "sessionId": SESSION, "agentId": "a1",
            "timestamp": (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": {"role": "user", "content": "result"}}


class TestAgentSummary(Base):
    KEY = "agent_summary:%s:agent-a1" % SESSION

    def msgs(self, subagent):
        return [rec("m%d" % i, off, [{"type": "text", "text": "hi"}], stop="end_turn",
                    cr=cr, subagent=subagent)
                for i, (off, cr) in enumerate([(0, 100), (10, 200), (40, 300), (100, 400)])]

    def test_one_aggregated_row_with_fire_count(self):
        p = self.path()
        self.write(p, self.msgs(True))
        row = self.parse(p)[self.KEY]
        # last activity t=100: ticks 30/60/90 count (120 is after the agent ended);
        # windows (0..30] {0,10} and (30..60] {40} fire, (60..90] is empty -> 2 fires
        self.assertEqual(row["input_tokens"], 2 * scanner.AGENT_SUMMARY_INPUT_TOKENS)
        self.assertEqual(row["output_tokens"], 2 * scanner.AGENT_SUMMARY_OUTPUT_TOKENS)
        # context = latest assistant message at or before each tick (t=10, t=40)
        self.assertEqual(row["cache_read_tokens"], (10 + 200) + (10 + 300))
        self.assertIsNone(row["tool_name"])
        self.assertEqual(row["is_sidechain"], 1)
        self.assertEqual(row["estimated"], 1)

    def test_main_thread_file_has_no_summary(self):
        p = self.path(subagent=False)
        self.write(p, self.msgs(False))
        self.assertFalse([k for k in self.parse(p) if k.startswith("agent_summary:")])

    def test_agent_finished_under_one_interval_gets_no_fire(self):
        p = self.path()
        self.write(p, self.msgs(True)[:2])  # t=0 and t=10
        self.assertFalse([k for k in self.parse(p) if k.startswith("agent_summary:")])

    def test_user_records_count_as_activity(self):
        p = self.path()
        # assistant at 0, tool_result at 50, assistant at 80: the tick at 60 fires
        # only because the user record landed in (30, 60]
        self.write(p, [rec("a", 0, [{"type": "text", "text": "x"}], stop="end_turn", cr=100),
                       user(50),
                       rec("b", 80, [{"type": "text", "text": "x"}], stop="end_turn", cr=500)])
        row = self.parse(p)[self.KEY]
        # ticks 30 {0}, 60 {50}; 90 > last activity (80). Both use message a.
        self.assertEqual(row["input_tokens"], 2 * scanner.AGENT_SUMMARY_INPUT_TOKENS)
        self.assertEqual(row["cache_read_tokens"], 2 * (10 + 100))

    def test_same_stem_in_two_sessions_does_not_collide(self):
        a, b = self.path(), self.proj / "p" / "other" / "subagents" / "agent-a1.jsonl"
        b.parent.mkdir(parents=True)
        self.write(a, self.msgs(True))
        rows = self.msgs(True)
        for r in rows:
            r["sessionId"] = "other"
        self.write(b, rows)
        keys = [k for k in self.scan() if k.startswith("agent_summary:")]
        self.assertEqual(len(keys), 2)


class TestIncremental(Base):
    def test_rescan_updates_summary_without_duplicates(self):
        p = self.path()
        first = [rec("m%d" % i, off, [{"type": "text", "text": "hi"}], stop="end_turn")
                 for i, off in enumerate([0, 10, 40])]
        self.write(p, first)
        os.utime(p, (1e9,) * 2)
        key = TestAgentSummary.KEY
        a = self.scan()
        self.assertEqual(a[key]["input_tokens"], 1 * 300)  # ticks <= 40: only 30
        # a streaming message, then a later message: new fire, and m3 stays one row
        self.write(p, [rec("m3", 100, [{"type": "text", "text": "z" * 600}])], mode="a")
        os.utime(p, (1e9 + 10,) * 2)
        b = self.scan()
        self.assertEqual(len(b), len(a) + 1)  # only m3 is new
        self.assertEqual(b[key]["input_tokens"], 2 * 300)
        self.assertEqual(b["m3"]["output_tokens"], 300)
        self.assertEqual(b["m3"]["estimated"], 1)
        # m3 finalizes with a real (lower) output: the estimate must not stick
        self.write(p, [rec("m3", 100, [{"type": "text", "text": "z" * 600}],
                           stop="end_turn", out=120)], mode="a")
        os.utime(p, (1e9 + 20,) * 2)
        c = self.scan()
        self.assertEqual(len(c), len(b))
        self.assertEqual(c["m3"]["output_tokens"], 120)
        self.assertEqual(c["m3"]["estimated"], 0)

    def test_user_only_append_moves_the_summary(self):
        p = self.path()
        self.write(p, [rec("a", 0, [{"type": "text", "text": "x"}], stop="end_turn"),
                       rec("b", 40, [{"type": "text", "text": "x"}], stop="end_turn")])
        os.utime(p, (1e9,) * 2)
        key = TestAgentSummary.KEY
        self.assertEqual(self.scan()[key]["input_tokens"], 1 * 300)
        self.write(p, [user(100)], mode="a")  # no new assistant turn
        os.utime(p, (1e9 + 10,) * 2)
        self.assertEqual(self.scan()[key]["input_tokens"], 2 * 300)  # ticks 30 and 60 now both <= last activity (100)


class TestUpsertPrecedence(Base):
    def setUp(self):
        super().setUp()
        p = self.path(subagent=False)
        self.write(p, [rec("m", 0, [{"type": "text", "text": "x"}], stop="end_turn",
                           out=100, subagent=False)])
        self.base = scanner.parse_jsonl_file(str(p))[1][0]
        self.conn = scanner.get_db(self.db)
        scanner.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def put(self, **kw):
        scanner.insert_turns(self.conn, [dict(self.base, **kw)])

    def row(self):
        return self.conn.execute("SELECT * FROM turns WHERE message_id = 'm'").fetchone()

    def test_estimate_never_replaces_a_real_final_row(self):
        self.put()  # real, stop_reason end_turn, output 100
        self.put(output_tokens=500, estimated=1, stop_reason="")
        self.assertEqual((self.row()["output_tokens"], self.row()["estimated"]), (100, 0))

    def test_estimate_may_raise_a_row_still_mid_stream(self):
        self.put(output_tokens=3, stop_reason="")  # pre-fix partial row
        self.put(output_tokens=500, estimated=1, stop_reason="")
        self.assertEqual((self.row()["output_tokens"], self.row()["estimated"]), (500, 1))

    def test_real_row_replaces_estimate_even_on_equal_counts(self):
        self.put(output_tokens=100, estimated=1, stop_reason="", cache_creation_tokens=0)
        self.put(cache_creation_tokens=7, cache_creation_5m_tokens=7)
        r = self.row()
        self.assertEqual((r["estimated"], r["cache_creation_tokens"],
                          r["cache_creation_5m_tokens"], r["stop_reason"]), (0, 7, 7, "end_turn"))

    def test_estimate_over_estimate_updates_when_values_move(self):
        self.put(estimated=1, stop_reason="", cache_read_tokens=10)
        self.put(estimated=1, stop_reason="", cache_read_tokens=20)
        self.assertEqual(self.row()["cache_read_tokens"], 20)


if __name__ == "__main__":
    unittest.main()
