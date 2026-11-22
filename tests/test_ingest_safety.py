"""Claude ingestion through the real scan path (`sources.scan_all`, temp transcripts,
temp DB): streaming-record precedence, forced re-parse, malformed records."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import sources
import sources.claude


def rec(mid, out, *, inp=10, cr=0, cc=0, cc1h=0, stop="", speed=None, ts="2026-11-01T10:00:00Z",
        content=None, sid="s1"):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": cc,
             "cache_creation": {"ephemeral_5m_input_tokens": cc - cc1h,
                                "ephemeral_1h_input_tokens": cc1h}}
    if speed:
        usage["speed"] = speed
    msg = {"model": "claude-sonnet-4-6", "usage": usage, "stop_reason": stop or None,
           "content": content or []}
    if mid:
        msg["id"] = mid
    return json.dumps({"type": "assistant", "sessionId": sid, "timestamp": ts,
                       "cwd": "/home/u/p", "message": msg})


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "projects", "proj")
        os.makedirs(self.proj)
        self.path = os.path.join(self.proj, "s1.jsonl")
        self.db = os.path.join(self.tmp, "usage.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *lines, mode="w"):
        with open(self.path, mode, encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in lines))
        # mtime resolution: make each write visible as a change
        st = os.stat(self.path)
        self._bump = getattr(self, "_bump", 0) + 10
        os.utime(self.path, (st.st_atime, st.st_mtime + self._bump))

    def scan(self):
        return sources.scan_all(db_path=self.db, verbose=False, sources=[
            sources.claude.ClaudeSource(projects_dirs=[os.path.join(self.tmp, "projects")])])

    def q(self, sql, *a):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, a).fetchall()
        finally:
            conn.close()

    def fresh(self):
        """Values a from-scratch scan of the same transcript stores."""
        os.remove(self.db)
        for ext in ("-wal", "-shm"):
            if os.path.exists(self.db + ext):
                os.remove(self.db + ext)
        self.scan()


class TestStreamingPrecedence(ScanCase):
    COLS = ("SELECT input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
            "cache_creation_1h_tokens, speed, stop_reason FROM turns WHERE message_id = 'm1'")

    def test_final_record_with_equal_output_replaces_partial(self):
        self.write(rec("m1", 100, cc=1000))
        self.scan()
        self.write(rec("m1", 100, cc=1000, cc1h=1000, speed="fast", stop="end_turn"), mode="a")
        self.scan()
        incremental = self.q(self.COLS)
        self.fresh()
        self.assertEqual(incremental, self.q(self.COLS))
        self.assertEqual(incremental[0][4:], (1000, "fast", "end_turn"))

    def test_larger_cache_read_on_equal_output_wins(self):
        self.write(rec("m1", 100, cr=1000, stop="end_turn"))
        self.scan()
        self.write(rec("m1", 100, cr=1000000, stop="end_turn"), mode="a")
        self.scan()
        self.assertEqual(self.q(self.COLS)[0][2], 1000000)

    def test_final_row_is_not_downgraded_by_a_partial_copy(self):
        self.write(rec("m1", 100, cr=500, stop="end_turn"))
        self.scan()
        other = os.path.join(self.proj, "s1-copy.jsonl")
        with open(other, "w", encoding="utf-8") as f:
            f.write(rec("m1", 100, cr=900) + "\n")  # partial, no stop_reason
        self.scan()
        self.assertEqual(self.q(self.COLS)[0][2], 500)


def tool(cid):
    return {"type": "tool_use", "id": cid, "name": "Read", "input": {"file_path": "/x"}}


class TestReparseAndRecords(ScanCase):
    def test_forced_reparse_keeps_one_copy_of_an_id_less_turn(self):
        self.write(rec("", 100, ts="2026-11-01T10:00:00Z"), rec("m2", 10, ts="2026-11-01T10:01:00Z"))
        self.scan()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM schema_meta WHERE key = 'merged_tool_calls_reparse_done_claude'")
        conn.commit(); conn.close()
        self.scan()
        self.assertEqual(self.q("SELECT COUNT(*), SUM(output_tokens) FROM turns"), [(2, 110)])
        self.assertEqual(self.q("SELECT turn_count, total_output_tokens FROM sessions"), [(2, 110)])

    def test_estimate_rows_survive_when_their_transcript_is_gone(self):
        os.remove(self.path) if os.path.exists(self.path) else None
        self.scan()  # empty but existing projects dir
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens, "
                     "cache_read_tokens, cache_creation_tokens, message_id, estimated) VALUES "
                     "('gone', '2026-10-01T00:00:00Z', 'claude-haiku-4-5', 1, 5, 0, 0, "
                     "'agent_summary:gone:agent-a', 1)")
        conn.execute("DELETE FROM schema_meta WHERE key = 'subagent_estimates_v2_reparse_done_claude'")
        conn.commit(); conn.close()
        self.scan()
        self.assertEqual(self.q("SELECT COUNT(*) FROM turns WHERE session_id = 'gone'"), [(1,)])

    def test_malformed_record_does_not_hide_later_turns(self):
        bad = json.dumps({"type": "assistant", "sessionId": "s1", "timestamp": "2026-11-01T10:00:30Z",
                          "message": "not-a-dict"})
        self.write(rec("m1", 10), bad, rec("m2", 20, ts="2026-11-01T10:01:00Z"))
        self.scan()
        self.assertEqual(self.q("SELECT message_id FROM turns ORDER BY message_id"), [("m1",), ("m2",)])

    def test_upgrade_reparse_corrects_a_stored_partial_and_keeps_id_less_turns(self):
        self.write(rec("m1", 100, cc=1000, cc1h=1000, speed="fast", stop="end_turn"),
                   rec("", 7, ts="2026-11-01T10:03:00Z"))
        self.scan()
        conn = sqlite3.connect(self.db)  # an old DB: the partial kept, marker unset
        conn.execute("UPDATE turns SET cache_creation_1h_tokens = 0, speed = '', stop_reason = '' "
                     "WHERE message_id = 'm1'")
        conn.execute("DELETE FROM schema_meta WHERE key = 'final_record_reparse_done_claude'")
        conn.commit(); conn.close()
        self.scan()
        self.assertEqual(self.q("SELECT cache_creation_1h_tokens, speed FROM turns WHERE message_id = 'm1'"),
                         [(1000, "fast")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM turns WHERE message_id = ''"), [(1,)])

    def test_a_parse_that_stops_early_is_reported_not_checkpointed(self):
        sources.PROGRESS["errors"] = []
        odd = json.loads(rec("m2", 20))
        odd["timestamp"] = 5  # not a string: compared with the first record's, the parse aborts
        self.write(rec("m1", 10), json.dumps(odd), rec("m3", 30, ts="2026-11-01T10:02:00Z"))
        self.scan()
        self.assertEqual(self.q("SELECT COUNT(*) FROM processed_files"), [(0,)])
        self.assertTrue(any(e["path"] == self.path for e in sources.PROGRESS["errors"]))

    def test_a_non_numeric_token_count_skips_only_that_field(self):
        odd = json.loads(rec("m2", 20, ts="2026-11-01T10:01:00Z"))
        odd["message"]["usage"]["input_tokens"] = "lots"
        self.write(rec("m1", 10), json.dumps(odd), rec("m3", 30, ts="2026-11-01T10:02:00Z"))
        self.scan()
        self.assertEqual(self.q("SELECT message_id, input_tokens FROM turns ORDER BY message_id"),
                         [("m1", 10), ("m2", 0), ("m3", 10)])

    def test_copies_each_with_one_billing_detail_do_not_swap_on_replay(self):
        a = os.path.join(self.proj, "a.jsonl")
        b = os.path.join(self.proj, "b.jsonl")
        stored = []
        for i, (path, line) in enumerate([(a, rec("m1", 100, cc=1000, cc1h=1000, stop="end_turn")),
                                          (b, rec("m1", 100, cc=1000, speed="fast", stop="end_turn"))] * 2):
            with open(path, "w", encoding="utf-8") as f:
                f.write(line + "\n")
            os.utime(path, (1e9 + i * 10, 1e9 + i * 10))
            self.scan()
            stored.append(self.q("SELECT cache_creation_1h_tokens, speed FROM turns WHERE message_id = 'm1'")[0])
        self.assertEqual(stored, [(1000, "")] * 4)  # a detail is never traded for another

    def test_lower_output_copy_does_not_drop_stored_calls(self):
        self.write(rec("m1", 100, content=[tool("a"), tool("b")], stop="tool_use"))
        self.scan()
        with open(os.path.join(self.proj, "s1-copy.jsonl"), "w", encoding="utf-8") as f:
            f.write(rec("m1", 50, content=[tool("a")]) + "\n")
        self.scan()
        calls = json.loads(self.q("SELECT tool_calls FROM turns WHERE message_id = 'm1'")[0][0])
        self.assertEqual([c["id"] for c in calls], ["a", "b"])

    def test_incremental_scan_picks_up_a_call_in_a_zero_usage_record(self):
        self.write(rec("m1", 100, content=[tool("a")]))
        self.scan()
        zero = json.loads(rec("m1", 0, inp=0, content=[tool("b")]))
        self.write(json.dumps(zero), rec("m2", 10, ts="2026-11-01T10:05:00Z"), mode="a")
        self.scan()
        calls = json.loads(self.q("SELECT tool_calls FROM turns WHERE message_id = 'm1'")[0][0])
        self.assertEqual([c["id"] for c in calls], ["a", "b"])

    def test_a_tail_of_only_a_zero_usage_record_still_adds_its_call(self):
        self.write(rec("m1", 100, content=[tool("a")]))
        self.scan()
        self.write(rec("m1", 0, inp=0, content=[tool("b")]), mode="a")
        self.scan()
        calls = json.loads(self.q("SELECT tool_calls FROM turns WHERE message_id = 'm1'")[0][0])
        self.assertEqual([c["id"] for c in calls], ["a", "b"])

    def test_restored_older_turn_moves_session_start_earlier(self):
        self.write(rec("m2", 10, ts="2026-11-07T10:00:00Z"))
        self.scan()
        with open(os.path.join(self.proj, "s1-restored.jsonl"), "w", encoding="utf-8") as f:
            f.write(rec("m1", 10, ts="2026-11-04T10:00:00Z") + "\n")
        self.scan()
        self.assertEqual(self.q("SELECT first_timestamp FROM sessions"), [("2026-11-04T10:00:00Z",)])

    def test_interrupted_scan_is_reconciled_by_the_retry(self):
        import scanner
        self.write(rec("m1", 50, stop="end_turn"))
        self.scan()
        self.write(rec("m1", 50, stop="end_turn"), rec("m1", 50, stop="end_turn"), mode="a")  # inflates the additive rollup
        with unittest.mock.patch.object(scanner, "reconcile_session_totals", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.scan()
        self.scan()
        self.assertEqual(self.q("SELECT turn_count, total_output_tokens FROM sessions"), [(1, 50)])


class TestBackupAndPaths(unittest.TestCase):
    def test_pre_migration_backup_holds_no_raw_tool_input(self):
        import scanner
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "u.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE turns (session_id TEXT, tool_calls TEXT)")
        conn.execute("INSERT INTO turns VALUES ('s', ?)", (json.dumps(
            [{"name": "Bash", "input": {"command": "echo PRIVATE-MARKER"}, "id": "c"}]),))
        conn.commit(); conn.close()
        conn = scanner.get_db(db)
        bak = scanner.backup_before_migration(conn)
        conn.close()
        self.assertIsNotNone(bak)
        with open(bak, "rb") as f:
            self.assertFalse(b"PRIVATE-MARKER" in f.read(), "raw input in the backup")

    def _raw_db(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = os.path.join(tmp, "u.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE turns (session_id TEXT, tool_calls TEXT)")
        conn.execute("CREATE TABLE sessions (session_id TEXT, topic TEXT, topic_source TEXT)")
        conn.execute("INSERT INTO turns VALUES ('s', ?)", (json.dumps(
            [{"name": "Bash", "input": {"command": "echo PRIVATE-MARKER"}, "id": "c"}]),))
        conn.execute("INSERT INTO sessions VALUES ('s', 'PROMPT-MARKER', 'prompt')")
        conn.commit(); conn.close()
        return tmp, db

    def test_raw_opt_in_still_drops_prompt_topics_from_the_backup(self):
        import scanner
        tmp, db = self._raw_db()
        conn = scanner.get_db(db)
        with unittest.mock.patch.dict(os.environ, {"WHYTOKENS_STORE_RAW_INPUTS": "1"}):
            import policy
            self.assertTrue(policy.store_raw_inputs(), "env name drifted")
            bak = scanner.backup_before_migration(conn)
        conn.close()
        with open(bak, "rb") as f:
            self.assertFalse(b"PROMPT-MARKER" in f.read(), "prompt topic in the backup")

    def test_a_failed_redaction_leaves_no_unredacted_copy(self):
        import scanner
        tmp, db = self._raw_db()
        conn = scanner.get_db(db)
        with unittest.mock.patch.object(scanner, "_redact_backup", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                scanner.backup_before_migration(conn)
        conn.close()
        self.assertEqual([n for n in os.listdir(tmp) if ".bak-" in n], [])

    def test_no_advisor_estimate_beside_a_stored_real_iteration(self):
        import scanner
        conn = scanner.get_db(":memory:")
        scanner.init_db(conn)
        base = dict(session_id="s", timestamp="2026-11-01T10:00:00Z", model="claude-fable-5",
                    input_tokens=1, output_tokens=5, cache_read_tokens=0, cache_creation_tokens=0,
                    tool_name="advisor", cwd="", tool_calls=None)
        scanner.insert_turns(conn, [dict(base, message_id="advisor:m:0")])
        scanner.insert_turns(conn, [dict(base, message_id="advisor:m:est", estimated=1, output_tokens=6000)])
        self.assertEqual([r[0] for r in conn.execute("SELECT message_id FROM turns")], ["advisor:m:0"])

    def test_worktree_discovery_never_probes_a_unc_path(self):
        import scanner
        from pathlib import Path
        scanner.worktree_main_root.cache_clear()
        with unittest.mock.patch.object(Path, "is_dir", side_effect=AssertionError("probed")), \
             unittest.mock.patch.object(Path, "is_file", side_effect=AssertionError("probed")):
            self.assertIsNone(scanner.worktree_main_root(r"\\fileserver\share\repo"))
            self.assertIsNone(scanner.worktree_main_root("//fileserver/share/repo"))


class TestScanBanner(unittest.TestCase):
    def test_status_carries_unread_compressed_count(self):
        import scanrun
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        per = {"claude": {"new": 1, "updated": 0, "skipped": 0, "turns": 1, "sessions": 1},
               "codex": {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0, "unreadable": 3}}
        with unittest.mock.patch.object(sources, "scan_all", return_value=per):
            st = scanrun.start(os.path.join(tmp, "u.db"), wait=True)
        self.assertEqual(st["result"]["unreadable"], 3)
        self.assertIn("zstd", st["unreadable_hint"])
        app = open(os.path.join(os.path.dirname(__file__), "..", "static", "modules", "app.js"),
                   encoding="utf-8").read()
        self.assertIn("sc.result.unreadable", app)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
