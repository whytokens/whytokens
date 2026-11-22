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


class TestAgentSummaryLongPrompt(Base):
    """Haiku 5.5 bills a request 5x once its prompt passes 100K. Summed into one row,
    short fires crossed that line together; each row must stay one fire's side of it."""
    KEY = TestAgentSummary.KEY

    def write_agent(self, model, crs):
        recs = []
        for i, cr in enumerate(crs):  # one message per 30 s window: one fire each
            r = rec("m%d" % i, i * 30, [{"type": "text", "text": "hi"}], stop="end_turn", cr=cr)
            r["message"]["model"] = model
            recs.append(r)
        recs.append(user(len(crs) * 30))  # keeps the last tick inside the agent's life
        self.write(self.path(), recs)

    def summaries(self, rows):
        return {k: v for k, v in rows.items() if k.startswith("agent_summary:")}

    def prompt(self, r):
        return r["input_tokens"] + r["cache_read_tokens"] + r["cache_creation_tokens"]

    def test_haiku_fires_grouped_under_the_line(self):
        self.write_agent("claude-haiku-5-5", [45_000, 45_000, 45_000, 150_000, 45_000])
        rows = self.summaries(self.parse(self.path()))
        # ticks 30..150 read m1, m2, m3 (150K), m4, m4
        self.assertEqual(sorted(rows), [self.KEY, self.KEY + "#1", self.KEY + "#2"])
        fires = [rows[k]["input_tokens"] // scanner.AGENT_SUMMARY_INPUT_TOKENS for k in sorted(rows)]
        self.assertEqual(fires, [2, 1, 2])
        self.assertEqual(self.prompt(rows[self.KEY + "#1"]), 300 + 10 + 150_000)  # alone, over the line
        for k in (self.KEY, self.KEY + "#2"):
            self.assertLessEqual(self.prompt(rows[k]), 100_000)
        self.assertEqual(sum(r["cache_read_tokens"] for r in rows.values()),
                         4 * 45_010 + 150_010)  # nothing lost or doubled

    def test_other_models_keep_one_row(self):
        self.write_agent("claude-sonnet-5-5", [45_000, 45_000, 45_000])
        self.assertEqual(list(self.summaries(self.parse(self.path()))), [self.KEY])

    def haiku(self, mid, off, cr, stop="end_turn"):
        r = rec(mid, off, [{"type": "text", "text": "hi"}], stop=stop, cr=cr)
        r["message"]["model"] = "claude-haiku-5-5"
        return r

    def rescan(self, p, records, n):
        self.write(p, records, mode="a")
        os.utime(p, (1e9 + n,) * 2)
        return self.summaries(self.scan())

    def test_regroup_on_rescan_drops_the_obsolete_row(self):
        p = self.path()
        # m1 still streaming at 60K: fires 60K | 40K+40K -> two rows
        self.write(p, [self.haiku("m0", 0, 45_000), self.haiku("m1", 30, 60_000, stop=None),
                       self.haiku("m2", 60, 40_000), user(90)])
        os.utime(p, (1e9,) * 2)
        self.assertEqual(sorted(self.summaries(self.scan())), [self.KEY, self.KEY + "#1"])
        # m1's final record says 10K: all three fires fit one row, so ":1" must go
        rows = self.rescan(p, [self.haiku("m1", 30, 10_000)], 10)
        self.assertEqual(list(rows), [self.KEY])
        self.assertEqual(rows[self.KEY]["input_tokens"], 3 * scanner.AGENT_SUMMARY_INPUT_TOKENS)

    def test_closed_row_is_dated_by_its_own_last_fire(self):
        p = self.path()
        self.write(p, [self.haiku("m0", 0, 45_000), self.haiku("m1", 30, 60_000),
                       self.haiku("m2", 50, 45_000)])  # one fire (tick 30); dated by m2, the latest
        os.utime(p, (1e9,) * 2)
        self.scan()
        # later fires open row ":1"; row 0 keeps its tokens but closes, dated by m1
        rows = self.rescan(p, [self.haiku("m3", 100, 45_000), user(130)], 10)
        self.assertEqual(sorted(rows), [self.KEY, self.KEY + "#1"])
        self.assertEqual(rows[self.KEY]["input_tokens"], scanner.AGENT_SUMMARY_INPUT_TOKENS)
        self.assertEqual(rows[self.KEY]["timestamp"], self.parse(p)[self.KEY]["timestamp"])
        self.assertEqual(rows[self.KEY]["timestamp"],
                         (T0 + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_rescan_of_one_file_keeps_its_siblings_rows(self):
        sid = "sess:one"  # a colon in the session id must not merge two files' rows
        files = {}
        for stem in ("agent-a", "agent-b"):
            f = self.proj / "p" / "x" / "subagents" / (stem + ".jsonl")
            f.parent.mkdir(parents=True, exist_ok=True)
            recs = [self.haiku("%s-m%d" % (stem, i), i * 30, 45_000) for i in range(3)] + [user(90)]
            for r in recs:
                r["sessionId"] = sid
            self.write(f, recs)
            os.utime(f, (1e9,) * 2)
            files[stem] = f
        before = sorted(self.summaries(self.scan()))
        self.assertEqual(len(before), 4)  # two rows per file
        tail = user(95)
        tail["sessionId"] = sid
        self.write(files["agent-b"], [tail], mode="a")
        os.utime(files["agent-b"], (1e9 + 10,) * 2)
        self.assertEqual(sorted(self.summaries(self.scan())), before)

    def test_rescan_never_touches_a_lookalike_owner(self):
        # every id below starts with the first file's; a rescan of the first keeps them
        files = []
        for sid, stem in (("sess", "agent-a"), ("sess:agent-a", "agent-b"), ("sess", "agent-a#x")):
            f = self.proj / "p" / sid.replace(":", "_") / "subagents" / (stem + ".jsonl")
            f.parent.mkdir(parents=True, exist_ok=True)
            recs = [self.haiku("%s-m%d" % (stem, i), i * 30, 45_000) for i in range(3)] + [user(90)]
            for r in recs:
                r["sessionId"] = sid
            self.write(f, recs)
            os.utime(f, (1e9,) * 2)
            files.append((f, sid))
        before = sorted(self.summaries(self.scan()))
        self.assertEqual(len(before), 6)
        tail = user(95)
        tail["sessionId"] = files[0][1]
        self.write(files[0][0], [tail], mode="a")
        os.utime(files[0][0], (1e9 + 10,) * 2)
        self.assertEqual(sorted(self.summaries(self.scan())), before)

    def test_rescan_with_no_fire_left_drops_the_row(self):
        p = self.path()
        self.write(p, [self.haiku("m0", 0, 1000, stop=None), user(40)])  # tick 30 fires
        os.utime(p, (1e9,) * 2)
        self.assertEqual(list(self.summaries(self.scan())), [self.KEY])
        # m0's final record lands at t=70: no message exists at or before any tick
        self.assertEqual(self.rescan(p, [self.haiku("m0", 70, 1000)], 10), {})

    def test_old_summed_row_is_rewritten_on_upgrade(self):
        import sources
        self.write_agent("claude-haiku-5-5", [45_000, 45_000, 45_000])
        src = [sources.claude.ClaudeSource(projects_dirs=[str(self.proj)])]
        sources.scan_all(db_path=self.db, verbose=False, sources=src)
        conn = scanner.get_db(self.db)  # simulate a pre-fix DB: one summed row, marker unset
        conn.execute("DELETE FROM turns WHERE message_id LIKE 'agent_summary:%#1'")
        conn.execute("UPDATE turns SET input_tokens=900, cache_read_tokens=135030 WHERE message_id=?",
                     (self.KEY,))
        conn.execute("DELETE FROM schema_meta WHERE key='agent_summary_long_prompt_reparse_done_claude'")
        conn.commit(); conn.close()
        sources.scan_all(db_path=self.db, verbose=False, sources=src)
        rows = self.summaries(self.scan())
        self.assertEqual(sorted(rows), [self.KEY, self.KEY + "#1"])
        self.assertTrue(all(self.prompt(r) <= 100_000 for r in rows.values()))


    def test_v1_row_is_replaced_on_upgrade_not_kept_beside(self):
        import sources
        self.write_agent("claude-haiku-5-5", [45_000, 45_000, 45_000])
        src = [sources.claude.ClaudeSource(projects_dirs=[str(self.proj)])]
        sources.scan_all(db_path=self.db, verbose=False, sources=src)
        conn = scanner.get_db(self.db)  # a v1 DB: the session-less key, marker unset
        conn.execute("UPDATE turns SET message_id = REPLACE(message_id, ?, 'agent_summary:') "
                     "WHERE message_id LIKE 'agent_summary:%'", ("agent_summary:%s:" % SESSION,))
        conn.execute("DELETE FROM schema_meta WHERE key='subagent_estimates_v2_reparse_done_claude'")
        conn.commit(); conn.close()
        sources.scan_all(db_path=self.db, verbose=False, sources=src)
        self.assertEqual(sorted(self.summaries(self.scan())), [self.KEY, self.KEY + "#1"])


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
