"""ingestion: per-call tool-result byte size + call id, per-turn prompt_seq,
and the backup taken before any schema change. Synthetic fixtures only."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import scanner
from scanner import get_db, init_db
from sources import ClaudeSource, scan_all

SID = "sess-tr"


def _rec(rtype, content, ts="2026-10-27T10:00:00Z", **extra):
    r = {"type": rtype, "sessionId": SID, "timestamp": ts, "cwd": "/tmp/p",
         "message": {"role": rtype, "content": content}}
    r.update(extra)
    return json.dumps(r)


def _asst(mid, content=None, ts="2026-10-27T10:00:01Z", **extra):
    r = {"type": "assistant", "sessionId": SID, "timestamp": ts, "cwd": "/tmp/p",
         "message": {"id": mid, "model": "claude-sonnet-4-6", "role": "assistant",
                     "content": content or [{"type": "text", "text": "ok"}],
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 10, "output_tokens": 5}}}
    r.update(extra)
    return json.dumps(r)


def _use(cid, name="Read"):
    return {"type": "tool_use", "id": cid, "name": name, "input": {}}


def _result(cid, content):
    return {"type": "tool_result", "tool_use_id": cid, "content": content}


def _prompt(text, **extra):
    return _rec("user", text, **extra)


class TestIsUserPrompt(unittest.TestCase):
    def check(self, record, expected):
        self.assertEqual(scanner.is_user_prompt(json.loads(record)), expected, record)

    def test_plain_prompt_counts(self):
        self.check(_prompt("fix the bug"), True)
        self.check(_rec("user", [{"type": "text", "text": "fix it"}]), True)

    def test_slash_command_counts(self):
        self.check(_prompt("<command-message>next-task</command-message>\n"
                           "<command-name>/next-task</command-name>"), True)

    def test_desktop_reminder_prefix_still_counts(self):
        self.check(_prompt("<system-reminder>\nctx\n</system-reminder>\n\ngo"), True)

    def test_non_prompts(self):
        for r in (
            _rec("user", [_result("t1", "x")]),
            _prompt("expanded skill body", isMeta=True),
            _prompt("This session is being continued...", isCompactSummary=True),
            _prompt("<system-reminder>\nonly a reminder\n</system-reminder>"),
            _prompt("<system-reminder>x</system-reminder>\n<side-sessions-event>e</side-sessions-event>"),
            _prompt("<system-reminder>x</system-reminder><cross-session-message from=a>m"),
            _prompt("<task-notification>\n<task-id>a</task-id>"),
            _prompt("<task-notification>x", origin={"kind": "task-notification"}),
            _prompt("hello", origin={"kind": "peer", "from": "a1"}),
            _prompt("hello", origin={"kind": "coordinator"}),
            _prompt("<local-command-stdout>Set model</local-command-stdout>"),
            _prompt("<local-command-caveat>Caveat</local-command-caveat>"),
            _rec("user", [{"type": "text", "text": "[Request interrupted by user]"}]),
            _asst("m1"),
        ):
            self.check(r, False)

    def test_image_only_prompt_counts(self):
        self.check(_rec("user", [{"type": "image", "source": {"type": "base64", "data": "x"}}]), True)

    def test_human_origin_counts(self):
        self.check(_prompt("<system-reminder>r</system-reminder>\nyes", origin={"kind": "human"}), True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "p").mkdir(parents=True)
        self.file = self.projects / "p" / f"{SID}.jsonl"
        self.db = self.tmp / "whytokens.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *lines, mode="w"):
        with open(self.file, mode, encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in lines))

    def scan(self):
        scan_all(db_path=self.db, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def q(self, sql, *args):
        conn = get_db(self.db)
        try:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def calls(self):
        return {r[0]: r[1:] for r in self.q(
            "SELECT call_id, session_id, message_id, tool_name, result_bytes FROM tool_results")}

    def seqs(self):
        return dict(self.q("SELECT message_id, prompt_seq FROM turns"))


class TestToolResults(Base):
    def test_sizes_ids_and_parallel_calls_split_across_records(self):
        # One message streamed as two records (one tool_use block each), then the
        # results: a string and a block list, with a 2-byte UTF-8 character.
        self.write(
            _prompt("go"),
            _asst("m1", [_use("c1", "Read")]),
            _asst("m1", [_use("c2", "Grep")]),
            _rec("user", [_result("c1", "héllo"),
                          _result("c2", [{"type": "text", "text": "abc"},
                                         {"type": "text", "text": "de"}])]),
            _asst("m2"),
        )
        self.scan()
        self.assertEqual(self.calls(), {
            "c1": (SID, "m1", "Read", 6),
            "c2": (SID, "m1", "Grep", 5),
        })

    def test_empty_result_is_zero_missing_result_is_null(self):
        self.write(_prompt("go"), _asst("m1", [_use("c1"), _use("c2")]),
                   _rec("user", [_result("c1", "")]))
        self.scan()
        c = self.calls()
        self.assertEqual(c["c1"][3], 0)
        self.assertIsNone(c["c2"][3])

    def test_result_arriving_in_a_later_scan(self):
        self.write(_prompt("go"), _asst("m1", [_use("c1", "Bash")]))
        self.scan()
        self.assertIsNone(self.calls()["c1"][3])
        self.write(_rec("user", [_result("c1", "1234")]), _asst("m2"), mode="a")
        self.scan()
        self.assertEqual(self.calls()["c1"], (SID, "m1", "Bash", 4))

    def test_non_text_block_counts_serialized_size(self):
        block = {"type": "image", "source": {"type": "base64", "data": "QUJD"}}
        self.write(_asst("m1", [_use("c1")]), _rec("user", [_result("c1", [block])]))
        self.scan()
        expected = len(json.dumps(block, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertEqual(self.calls()["c1"][3], expected)


class TestReviewFindings(Base):
    def sizes(self):
        conn = get_db(self.db)
        try:
            return scanner.session_result_bytes(conn)
        finally:
            conn.close()

    def test_unresolved_streamed_call_makes_session_unknown(self):
        # tool_calls on the kept turn lists only the last record's call, so the
        # unresolved one must be caught from tool_results itself.
        self.write(_prompt("go"), _asst("m1", [_use("c1")]), _asst("m1", [_use("c2")]),
                   _rec("user", [_result("c2", "abcd")]), _asst("m1"))
        self.scan()
        self.assertIsNone(self.sizes()[SID])

    def test_branch_copy_keeps_the_first_session(self):
        lines = [_prompt("go"), _asst("m1", [_use("c1")]), _rec("user", [_result("c1", "abcd")])]
        self.write(*lines)
        (self.projects / "p" / "zz-copy.jsonl").write_text(
            "".join(l.replace(SID, "sess-copy") + "\n" for l in lines), encoding="utf-8")
        self.scan()
        self.assertEqual(self.calls()["c1"][0], SID)
        self.assertEqual(self.q("SELECT session_id FROM turns WHERE message_id='m1'"), [(SID,)])
        self.assertEqual(self.sizes()[SID], 4)

    def test_backfill_reading_a_copy_first_keeps_the_turns_session(self):
        lines = [_prompt("go"), _asst("m1", [_use("c1")]), _rec("user", [_result("c1", "abcd")])]
        self.write(*lines)
        self.scan()
        conn = get_db(self.db)
        conn.execute("DELETE FROM tool_results")
        conn.execute("DELETE FROM schema_meta WHERE key = 'tool_results_backfill_done_claude'")
        conn.commit()
        conn.close()
        # A branch copy that sorts first, so the backfill reads it before the original.
        (self.projects / "p" / "aa-copy.jsonl").write_text(
            "".join(l.replace(SID, "sess-copy") + "\n" for l in lines), encoding="utf-8")
        self.scan()
        self.assertEqual(self.q("SELECT session_id FROM turns WHERE message_id='m1'"), [(SID,)])
        self.assertEqual(self.calls()["c1"][0], SID)
        self.assertEqual(self.sizes()[SID], 4)

    def test_backfill_gives_estimate_rows_a_sequence(self):
        adv = {"type": "server_tool_use", "id": "s1", "name": "advisor", "input": {}}
        self.write(_prompt("go"), _asst("m1", [adv]))
        self.scan()
        conn = get_db(self.db)
        conn.execute("UPDATE turns SET prompt_seq = NULL")
        conn.execute("DELETE FROM schema_meta WHERE key = 'tool_results_backfill_done_claude'")
        conn.commit()
        conn.close()
        self.scan()
        self.assertEqual(self.seqs().get("advisor:m1:est"), 1)

    def test_incremental_id_less_turn_gets_its_sequence(self):
        self.write(_prompt("a"), _asst("t1"))
        self.scan()
        rec = json.loads(_asst(""))
        del rec["message"]["id"]
        self.write(_prompt("b"), json.dumps(rec), mode="a")
        self.scan()
        self.assertEqual(self.q("SELECT prompt_seq FROM turns WHERE COALESCE(message_id, '') = ''"),
                         [(2,)])


class TestPromptSeq(Base):
    def test_sequence_skips_non_prompts(self):
        self.write(
            _prompt("first"),
            _asst("t1", [_use("c1")]),
            _rec("user", [_result("c1", "r")]),
            _asst("t2"),
            _prompt("skill body", isMeta=True),
            _prompt("summary", isCompactSummary=True),
            _asst("t2b"),
            _prompt("<system-reminder>r</system-reminder>\n\nsecond"),
            _asst("t3"),
            _prompt("<command-name>/next-task</command-name>"),
            _asst("t4"),
            _prompt("<task-notification>done", origin={"kind": "task-notification"}),
            _asst("t5"),
            _prompt("<system-reminder>r</system-reminder><side-sessions-event>x"),
            _rec("user", [{"type": "text", "text": "[Request interrupted by user]"}]),
            _asst("t6"),
        )
        self.scan()
        self.assertEqual(self.seqs(), {"t1": 1, "t2": 1, "t2b": 1, "t3": 2,
                                       "t4": 3, "t5": 3, "t6": 3})

    def test_turn_before_any_prompt_is_zero(self):
        self.write(_asst("t0"), _prompt("go"), _asst("t1"))
        self.scan()
        self.assertEqual(self.seqs(), {"t0": 0, "t1": 1})

    def test_incremental_scan_keeps_counting(self):
        self.write(_prompt("a"), _asst("t1"), _prompt("b"), _asst("t2"))
        self.scan()
        self.write(_prompt("c"), _asst("t3"), mode="a")
        self.scan()
        self.assertEqual(self.seqs(), {"t1": 1, "t2": 2, "t3": 3})

    def test_subagent_turns_are_null(self):
        sub = self.projects / "p" / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(
            _prompt("dispatch prompt", isSidechain=True) + "\n"
            + _asst("s1", isSidechain=True, agentId="a1") + "\n", encoding="utf-8")
        self.write(_prompt("go"), _asst("t1"))
        self.scan()
        self.assertEqual(self.seqs()["t1"], 1)
        self.assertIsNone(self.seqs()["s1"])

    def test_advisor_turn_inherits_parent_seq(self):
        rec = json.loads(_asst("t1"))
        rec["advisorModel"] = "claude-fable-5"
        rec["message"]["usage"]["iterations"] = [
            {"type": "advisor_message", "input_tokens": 7, "output_tokens": 3}]
        self.write(_prompt("go"), json.dumps(rec))
        self.scan()
        self.assertEqual(self.seqs()["advisor:t1:0"], 1)


class TestBackfillAndUnknown(Base):
    """An upgraded DB: rows from before per-call ingestion gain prompt_seq and call sizes once,
    from transcripts that still exist. Pruned sessions stay unknown (NULL)."""

    def setUp(self):
        super().setUp()
        self.write(_prompt("go"), _asst("m1", [_use("c1")]),
                   _rec("user", [_result("c1", "abcd")]))
        self.scan()
        conn = get_db(self.db)
        # Make it look like an older DB: no seq, no call rows.
        conn.execute("UPDATE turns SET prompt_seq = NULL")
        conn.execute("DELETE FROM tool_results")
        conn.execute("DELETE FROM schema_meta WHERE key LIKE 'tool_results_backfill%'")
        # A pruned session: a turn with a tool call whose transcript is gone.
        conn.execute("INSERT INTO sessions (session_id, agent) VALUES ('gone', 'claude')")
        conn.execute("INSERT INTO turns (session_id, message_id, model, agent, tool_calls) "
                     "VALUES ('gone', 'mg', 'claude-sonnet-4-6', 'claude', ?)",
                     (json.dumps([{"name": "Read", "input": {}}]),))
        # A measured session with no tool calls at all, and a Codex session.
        conn.execute("INSERT INTO sessions (session_id, agent) VALUES ('quiet', 'claude')")
        conn.execute("INSERT INTO turns (session_id, message_id, model, agent) "
                     "VALUES ('quiet', 'mq', 'claude-sonnet-4-6', 'claude')")
        conn.execute("INSERT INTO sessions (session_id, agent) VALUES ('cx', 'codex')")
        conn.execute("INSERT INTO turns (session_id, message_id, model, agent) "
                     "VALUES ('cx', 'mc', 'gpt-5', 'codex')")
        conn.commit()
        conn.close()

    def test_backfill_fills_existing_rows(self):
        self.scan()
        self.assertEqual(self.seqs()["m1"], 1)
        self.assertEqual(self.calls()["c1"][3], 4)

    def test_pruned_session_is_null_never_zero(self):
        self.scan()
        conn = get_db(self.db)
        sizes = scanner.session_result_bytes(conn)
        conn.close()
        self.assertEqual(sizes[SID], 4)
        self.assertIsNone(sizes["gone"])
        self.assertEqual(sizes["quiet"], 0)
        self.assertIsNone(sizes["cx"])  # Codex: not measurable
        self.assertIsNone(self.seqs()["mg"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM tool_results WHERE session_id='gone'"), [(0,)])

    def test_unmatched_call_makes_session_unknown(self):
        self.scan()
        conn = get_db(self.db)
        conn.execute("UPDATE tool_results SET result_bytes = NULL")
        conn.commit()
        self.assertIsNone(scanner.session_result_bytes(conn)[SID])
        conn.close()


def _bak(db):
    return Path(f"{db}.bak-{datetime.now():%Y%m%d}")


class TestBackupBeforeMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "whytokens.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old_db(self):
        conn = get_db(self.db)
        init_db(conn)
        conn.execute("INSERT INTO turns (session_id, message_id) VALUES ('s', 'm')")
        conn.execute("ALTER TABLE turns DROP COLUMN prompt_seq")
        conn.execute("DROP TABLE tool_results")
        conn.commit()
        conn.close()

    @staticmethod
    def _cols(path):
        c = sqlite3.connect(path)
        try:
            return ({r[1] for r in c.execute("PRAGMA table_info(turns)")},
                    c.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                    {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            c.close()

    def test_old_db_is_backed_up_before_migrating(self):
        self._old_db()
        conn = get_db(self.db)
        init_db(conn)
        conn.close()
        bak = _bak(self.db)
        self.assertTrue(bak.exists())
        cols, rows, tables = self._cols(bak)
        self.assertNotIn("prompt_seq", cols)       # taken before the ALTER
        self.assertNotIn("tool_results", tables)   # and before the CREATE
        self.assertEqual(rows, 1)
        self.assertIn("prompt_seq", self._cols(self.db)[0])
        c = sqlite3.connect(self.db)
        self.assertEqual(c.execute("SELECT prompt_seq FROM turns").fetchall(), [(None,)])
        c.close()

    def test_missing_column_alone_triggers_backup(self):
        self._old_db()
        conn = get_db(self.db)
        conn.execute("CREATE TABLE tool_results (call_id TEXT PRIMARY KEY, session_id TEXT, "
                     "message_id TEXT, tool_name TEXT, result_bytes INTEGER)")
        conn.execute("CREATE INDEX idx_tool_results_message ON tool_results(message_id)")
        conn.execute("CREATE INDEX idx_tool_results_session ON tool_results(session_id)")
        conn.commit()
        init_db(conn)
        conn.close()
        self.assertNotIn("prompt_seq", self._cols(_bak(self.db))[0])

    def test_no_backup_for_fresh_or_current_db(self):
        conn = get_db(self.db)
        init_db(conn)
        init_db(conn)
        conn.close()
        self.assertFalse(_bak(self.db).exists())

    def test_missing_index_triggers_backup(self):
        conn = get_db(self.db)
        init_db(conn)
        conn.execute("INSERT INTO turns (session_id, message_id) VALUES ('s', 'm')")
        conn.execute("DROP INDEX idx_turns_timestamp")
        conn.commit()
        init_db(conn)
        conn.close()
        self.assertTrue(_bak(self.db).exists())

    def test_existing_same_day_backup_is_kept(self):
        self._old_db()
        _bak(self.db).write_bytes(b"first")
        conn = get_db(self.db)
        init_db(conn)
        conn.close()
        self.assertEqual(_bak(self.db).read_bytes(), b"first")

    def test_failed_backup_blocks_migration(self):
        self._old_db()
        os.makedirs(f"{_bak(self.db)}.tmp")  # the temp target can't be opened as a file
        conn = get_db(self.db)
        with self.assertRaises(Exception):
            init_db(conn)
        conn.close()
        self.assertNotIn("prompt_seq", self._cols(self.db)[0])


if __name__ == "__main__":
    unittest.main()
