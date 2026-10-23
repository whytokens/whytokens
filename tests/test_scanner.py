"""Tests for scanner.py - JSONL parsing, DB operations, and scanning."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import scanner
from scanner import (
    get_db, init_db, project_name_from_cwd, parse_jsonl_file,
    aggregate_sessions, upsert_sessions, insert_turns, scan,
    reconcile_session_models, _model_priority, backfill_topics_once,
)


class TestProjectNameFromCwd(unittest.TestCase):
    def test_two_components(self):
        self.assertEqual(project_name_from_cwd("/home/user/myproject"), "user/myproject")

    def test_deep_path(self):
        self.assertEqual(project_name_from_cwd("/a/b/c/d"), "c/d")

    def test_single_component(self):
        self.assertEqual(project_name_from_cwd("/root"), "/root")

    def test_windows_path(self):
        self.assertEqual(project_name_from_cwd("C:\\Users\\me\\project"), "me/project")

    def test_trailing_slash(self):
        self.assertEqual(project_name_from_cwd("/home/user/project/"), "user/project")

    def test_empty_string(self):
        self.assertEqual(project_name_from_cwd(""), "unknown")

    def test_none(self):
        self.assertEqual(project_name_from_cwd(None), "unknown")


def _make_assistant_record(session_id="sess-1", model="claude-sonnet-4-6",
                           input_tokens=100, output_tokens=50,
                           cache_read=10, cache_creation=5,
                           timestamp="2026-04-08T10:00:00Z",
                           cwd="/home/user/project",
                           message_id="", duration_ms=0, stop_reason="",
                           service_tier="", inference_geo="",
                           cache_creation_5m=0, cache_creation_1h=0,
                           is_sidechain=False, is_compact_summary=False,
                           effort=None):
    msg = {
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_creation_5m,
                "ephemeral_1h_input_tokens": cache_creation_1h,
            },
            "service_tier": service_tier,
            "inference_geo": inference_geo,
        },
        "stop_reason": stop_reason,
        "content": [],
    }
    if message_id:
        msg["id"] = message_id
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "durationMs": duration_ms,
        "stopReason": stop_reason,
        "isSidechain": is_sidechain,
        "isCompactSummary": is_compact_summary,
        "message": msg,
    }
    # Only present from Claude Code 2.1.215; omitted entirely when not asked for,
    # so the default record still exercises the pre-2.1.215 shape.
    if effort is not None:
        record["effort"] = effort
    return json.dumps(record)


def _make_user_record(session_id="sess-1", timestamp="2026-04-08T09:59:00Z",
                      cwd="/home/user/project"):
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    })


class TestParseJsonlFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_basic_parsing(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(),
            _make_assistant_record(),
        ])
        metas, turns, line_count = parse_jsonl_file(path)
        self.assertEqual(len(metas), 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(metas[0]["session_id"], "sess-1")
        self.assertEqual(turns[0]["input_tokens"], 100)
        self.assertEqual(turns[0]["output_tokens"], 50)
        self.assertEqual(line_count, 2)

    def test_skips_zero_token_records(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(input_tokens=0, output_tokens=0,
                                   cache_read=0, cache_creation=0),
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 0)

    def test_skips_non_assistant_user_types(self):
        path = self._write_jsonl("test.jsonl", [
            json.dumps({"type": "system", "sessionId": "s1"}),
            _make_assistant_record(session_id="s1"),
        ])
        metas, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_handles_malformed_json(self):
        path = self._write_jsonl("test.jsonl", [
            "not valid json",
            _make_assistant_record(),
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_handles_empty_file(self):
        path = self._write_jsonl("test.jsonl", [])
        metas, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(metas), 0)
        self.assertEqual(len(turns), 0)

    def test_multiple_sessions(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(session_id="s1"),
            _make_assistant_record(session_id="s2"),
        ])
        metas, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(metas), 2)
        self.assertEqual(len(turns), 2)

    def test_session_timestamps_tracked(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(timestamp="2026-04-08T09:00:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:05:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:10:00Z"),
        ])
        metas, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["first_timestamp"], "2026-04-08T09:00:00Z")
        self.assertEqual(metas[0]["last_timestamp"], "2026-04-08T09:10:00Z")

    def test_tool_name_extracted(self):
        record = json.dumps({
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-04-08T10:00:00Z",
            "cwd": "/tmp",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "name": "Read"}],
            },
        })
        path = self._write_jsonl("test.jsonl", [record])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["tool_name"], "Read")

    def test_extended_turn_metadata_extracted(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(
                duration_ms=1234,
                stop_reason="tool_use",
                service_tier="standard",
                inference_geo="not_available",
                cache_creation_5m=7,
                cache_creation_1h=11,
                is_sidechain=True,
                is_compact_summary=True,
            )
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["cache_creation_5m_tokens"], 7)
        self.assertEqual(turns[0]["cache_creation_1h_tokens"], 11)
        self.assertEqual(turns[0]["duration_ms"], 1234)
        self.assertEqual(turns[0]["stop_reason"], "tool_use")
        self.assertEqual(turns[0]["service_tier"], "standard")
        self.assertEqual(turns[0]["inference_geo"], "not_available")
        self.assertEqual(turns[0]["is_sidechain"], 1)
        self.assertEqual(turns[0]["is_compact_summary"], 1)

    def test_effort_extracted(self):
        path = self._write_jsonl("effort.jsonl", [
            _make_assistant_record(message_id="m-eff", effort="high")
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["effort"], "high")

    def test_effort_absent_is_empty_string(self):
        # Pre-2.1.215 records have no `effort` key at all — must not be None,
        # so NULL vs '' can't split one logical bucket in two.
        path = self._write_jsonl("no-effort.jsonl", [
            _make_assistant_record(message_id="m-noeff")
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["effort"], "")


class TestMessageIdDedup(unittest.TestCase):
    """Test deduplication of streaming events by message.id."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_streaming_events_deduped(self):
        """Multiple records with same message.id should produce one turn."""
        path = self._write_jsonl("test.jsonl", [
            # Streaming event 1: partial usage
            _make_assistant_record(message_id="msg-abc", input_tokens=50, output_tokens=10),
            # Streaming event 2: more usage (same message)
            _make_assistant_record(message_id="msg-abc", input_tokens=100, output_tokens=50),
            # Streaming event 3: final usage (same message)
            _make_assistant_record(message_id="msg-abc", input_tokens=150, output_tokens=80),
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)
        # Last record wins (has final tallies)
        self.assertEqual(turns[0]["input_tokens"], 150)
        self.assertEqual(turns[0]["output_tokens"], 80)
        self.assertEqual(turns[0]["message_id"], "msg-abc")

    def test_different_message_ids_kept(self):
        """Records with different message.id are separate turns."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(message_id="msg-1", input_tokens=100),
            _make_assistant_record(message_id="msg-2", input_tokens=200),
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)

    def test_records_without_message_id_kept(self):
        """Records without message.id are kept as-is (no dedup)."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(input_tokens=100),
            _make_assistant_record(input_tokens=200),
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)

    def test_mixed_with_and_without_ids(self):
        """Mix of records with and without message.id."""
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(message_id="msg-1", input_tokens=50),
            _make_assistant_record(message_id="msg-1", input_tokens=100),  # deduped
            _make_assistant_record(input_tokens=200),  # no id, kept
        ])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 2)  # 1 deduped + 1 without id
        token_sums = sorted([t["input_tokens"] for t in turns])
        self.assertEqual(token_sums, [100, 200])


class TestMessageIdDedupIntegration(unittest.TestCase):
    """Integration test: dedup across scan cycles."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def test_streaming_dedup_reduces_turn_count(self):
        """3 streaming events for 2 messages should produce 2 turns."""
        with open(self.filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=50, output_tokens=20) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=100, output_tokens=50) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-2",
                                           input_tokens=200, output_tokens=100) + "\n")

        result = scan(projects_dir=self.projects_dir.parent.parent,
                      db_path=self.db_path, verbose=False)
        self.assertEqual(result["turns"], 2)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        turns = conn.execute("SELECT * FROM turns ORDER BY input_tokens").fetchall()
        self.assertEqual(len(turns), 2)
        # msg-1: last record wins (100/50), msg-2: 200/100
        self.assertEqual(turns[0]["input_tokens"], 100)
        self.assertEqual(turns[1]["input_tokens"], 200)
        # Session totals should reflect deduped values
        session = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(session["total_input_tokens"], 300)  # 100 + 200
        conn.close()

    def test_cross_file_dedup_via_unique_index(self):
        """Re-scanning a file shouldn't create duplicate turns for same message_id."""
        with open(self.filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           message_id="msg-1",
                                           input_tokens=100, output_tokens=50) + "\n")

        scan(projects_dir=self.projects_dir.parent.parent,
             db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        count1 = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        self.assertEqual(count1, 1)

        # Delete processed_files to force re-scan
        conn.execute("DELETE FROM processed_files")
        conn.commit()
        conn.close()

        scan(projects_dir=self.projects_dir.parent.parent,
             db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        count2 = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        # Should still be 1 turn (UNIQUE index prevents duplicate)
        self.assertEqual(count2, 1)

    def test_schema_migration_adds_turn_metadata_columns(self):
        """Existing DBs without newer turn columns should be upgraded."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                timestamp TEXT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                tool_name TEXT,
                cwd TEXT
            );
        """)
        conn.commit()
        conn.close()

        # init_db should add newer turn metadata columns
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        init_db(conn)
        # Verify column exists
        row = conn.execute("PRAGMA table_info(turns)").fetchall()
        col_names = [r["name"] for r in row]
        for col in [
            "message_id", "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            "duration_ms", "stop_reason", "service_tier", "inference_geo",
            "is_sidechain", "is_compact_summary", "effort",
        ]:
            self.assertIn(col, col_names)
        conn.close()


class TestAggregateSessions(unittest.TestCase):
    def test_aggregation(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        turns = [
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 10, "cache_creation_tokens": 5, "model": "claude-sonnet-4-6"},
            {"session_id": "s1", "input_tokens": 200, "output_tokens": 100,
             "cache_read_tokens": 20, "cache_creation_tokens": 10, "model": "claude-sonnet-4-6"},
        ]
        sessions = aggregate_sessions(metas, turns)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 300)
        self.assertEqual(sessions[0]["total_output_tokens"], 150)
        self.assertEqual(sessions[0]["turn_count"], 2)
        self.assertEqual(sessions[0]["model"], "claude-sonnet-4-6")

    def test_empty_turns(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        sessions = aggregate_sessions(metas, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 0)
        self.assertEqual(sessions[0]["turn_count"], 0)


def _model_turn(session_id, model, is_sidechain, message_id, tokens=100):
    """Minimal turn dict for insert_turns, varying only model/sidechain/id."""
    return {
        "session_id": session_id, "timestamp": "2026-06-11T00:00:00Z",
        "model": model, "input_tokens": tokens, "output_tokens": tokens,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
        "tool_name": None, "cwd": "/p", "message_id": message_id,
        "duration_ms": 0, "stop_reason": "", "service_tier": "",
        "inference_geo": "", "is_sidechain": is_sidechain,
        "is_compact_summary": 0, "tool_calls": None, "agent": "claude",
        "reasoning_tokens": 0,
    }


class TestSidechainModelSelection(unittest.TestCase):
    """A subagent (sidechain) running on a different model than the main chain must
    not define the session's primary-model label. Regression for a fable-5 session
    that spawned an opus Task being mislabeled opus."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_fable_outranks_opus_in_priority(self):
        # fable-5 is the current top tier; omitting it (the bug) scored it below
        # haiku, so any priority-ranked subagent turn outranked the real model.
        self.assertGreater(_model_priority("claude-fable-5"),
                           _model_priority("claude-opus-4-8"))
        self.assertGreater(_model_priority("claude-opus-4-8"),
                           _model_priority("claude-haiku-4-5"))

    def test_mythos_ranks_above_opus(self):
        # mythos is ranked equal to fable (upstream tier), above opus.
        self.assertGreater(_model_priority("claude-mythos-5"),
                           _model_priority("claude-opus-4-8"))
        self.assertEqual(_model_priority("claude-mythos-5"),
                         _model_priority("claude-fable-5"))

    def test_aggregate_ignores_sidechain_for_model(self):
        metas = [{"session_id": "s1", "project_name": "p", "first_timestamp": "t1",
                  "last_timestamp": "t2", "git_branch": "main", "model": None}]
        # One fable MAIN turn, two opus SIDECHAIN turns. Sidechains outnumber the
        # main turn, but the label must follow the main chain.
        turns = [
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "model": "claude-fable-5", "is_sidechain": 0},
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "model": "claude-opus-4-8", "is_sidechain": 1},
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "model": "claude-opus-4-8", "is_sidechain": 1},
        ]
        sessions = aggregate_sessions(metas, turns)
        self.assertEqual(sessions[0]["model"], "claude-fable-5")
        # All three turns' tokens are still summed — cost accounting is unaffected.
        self.assertEqual(sessions[0]["turn_count"], 3)
        self.assertEqual(sessions[0]["total_output_tokens"], 150)

    def test_reconcile_corrects_mislabeled_session(self):
        insert_turns(self.conn, [
            _model_turn("s1", "claude-fable-5", 0, "m1"),
            _model_turn("s1", "claude-fable-5", 0, "m2"),
            _model_turn("s1", "claude-opus-4-8", 1, "m3"),  # subagent
        ])
        # Seed the buggy label a pre-fix scan would have written.
        self.conn.execute(
            "INSERT INTO sessions (session_id, project_name, model, turn_count) "
            "VALUES ('s1', 'p', 'claude-opus-4-8', 3)")
        self.conn.commit()

        reconcile_session_models(self.conn)

        model = self.conn.execute(
            "SELECT model FROM sessions WHERE session_id='s1'").fetchone()["model"]
        self.assertEqual(model, "claude-fable-5")

    def test_reconcile_leaves_sidechain_only_session_untouched(self):
        # A subagent file parsed before its parent: every turn is a sidechain, so
        # reconcile must not invent a label from them — keep what's there.
        insert_turns(self.conn, [
            _model_turn("s1", "claude-opus-4-8", 1, "m1"),
            _model_turn("s1", "claude-opus-4-8", 1, "m2"),
        ])
        self.conn.execute(
            "INSERT INTO sessions (session_id, project_name, model, turn_count) "
            "VALUES ('s1', 'p', 'claude-opus-4-8', 2)")
        self.conn.commit()

        reconcile_session_models(self.conn)

        model = self.conn.execute(
            "SELECT model FROM sessions WHERE session_id='s1'").fetchone()["model"]
        self.assertEqual(model, "claude-opus-4-8")


def _dispatch_record(session_id="sess-1", agent_id="ag-1", agent_type="Explore",
                     timestamp="2026-04-08T10:05:00Z"):
    """Parent-side user tool_result that closes a Task/Agent dispatch."""
    return json.dumps({
        "type": "user", "sessionId": session_id, "timestamp": timestamp,
        "toolUseResult": {
            "agentId": agent_id, "agentType": agent_type, "status": "completed",
            "totalTokens": 4096, "totalDurationMs": 18000, "totalToolUseCount": 11,
        },
        "message": {"role": "user", "content": [{"type": "tool_result"}]},
    })


class TestSubagentDispatch(unittest.TestCase):
    """Subagent dispatch capture: agents table + turns.agent_id + is_sidechain."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, lines):
        path = os.path.join(self.tmpdir, "s.jsonl")
        with open(path, "w") as f:
            for ln in lines:
                f.write(ln + "\n")
        return path

    def test_extract_agent_dispatch_from_tool_result(self):
        rec = json.loads(_dispatch_record())
        d = scanner.extract_agent_dispatch(rec)
        self.assertEqual(d["agent_id"], "ag-1")
        self.assertEqual(d["agent_type"], "Explore")
        self.assertEqual(d["total_tokens"], 4096)
        self.assertEqual(d["tool_use_count"], 11)

    def test_non_dispatch_user_record_ignored(self):
        self.assertIsNone(scanner.extract_agent_dispatch(json.loads(_make_user_record())))

    def test_agent_id_only_dispatch_still_captured(self):
        # Async dispatches often close out with agentId but no agentType — must not
        # be silently dropped (agent_type comes back None, displayed as 'unknown').
        rec = {
            "type": "user", "sessionId": "sess-1", "timestamp": "2026-04-08T10:05:00Z",
            "toolUseResult": {
                "agentId": "ag-async", "status": "completed",
                "totalTokens": 512, "totalDurationMs": 4000, "totalToolUseCount": 3,
            },
        }
        d = scanner.extract_agent_dispatch(rec)
        self.assertIsNotNone(d)
        self.assertEqual(d["agent_id"], "ag-async")
        self.assertIsNone(d["agent_type"])
        self.assertEqual(d["total_tokens"], 512)
        self.assertEqual(d["tool_use_count"], 3)

    def test_agents_out_populated_by_parse(self):
        path = self._write([
            _dispatch_record(agent_id="ag-9", agent_type="general-purpose"),
            _make_assistant_record(session_id="sess-1", message_id="m1"),
        ])
        agents = {}
        _, _, _ = parse_jsonl_file(path, agents_out=agents)
        self.assertIn("ag-9", agents)
        self.assertEqual(agents["ag-9"]["agent_type"], "general-purpose")

    def test_subagent_turn_flagged_and_agent_id_captured(self):
        # An assistant turn carrying agentId is a subagent even without isSidechain.
        rec = json.loads(_make_assistant_record(session_id="sub", message_id="ms1"))
        rec["agentId"] = "ag-1"
        path = self._write([json.dumps(rec)])
        _, turns, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["agent_id"], "ag-1")
        self.assertEqual(turns[0]["is_sidechain"], 1)

    def test_scan_writes_agents_table(self):
        projects = Path(self.tmpdir) / "projects" / "user/p"
        projects.mkdir(parents=True)
        with open(projects / "sess-1.jsonl", "w") as f:
            f.write(_dispatch_record(agent_id="ag-x", agent_type="Explore") + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="m1") + "\n")
        db = Path(self.tmpdir) / "u.db"
        scanner.scan(projects_dir=Path(self.tmpdir) / "projects", db_path=db, verbose=False)
        conn = get_db(db)
        row = conn.execute(
            "SELECT agent_type, total_tokens, tool_use_count FROM agents WHERE agent_id='ag-x'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["agent_type"], "Explore")
        self.assertEqual(row["total_tokens"], 4096)


class TestDatabaseOperations(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_init_db_creates_tables(self):
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        self.assertIn("sessions", table_names)
        self.assertIn("turns", table_names)
        self.assertIn("processed_files", table_names)

    def test_init_db_is_idempotent(self):
        # Running init_db twice should not error
        init_db(self.conn)
        init_db(self.conn)

    def test_upsert_new_session(self):
        sessions = [{
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }]
        upsert_sessions(self.conn, sessions)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertEqual(row["turn_count"], 5)

    def test_upsert_updates_existing_session(self):
        session = {
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }
        upsert_sessions(self.conn, [session])
        # Add more tokens
        session2 = {**session, "total_input_tokens": 200, "total_output_tokens": 100,
                    "total_cache_read": 20, "total_cache_creation": 10, "turn_count": 2}
        upsert_sessions(self.conn, [session2])
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertEqual(row["total_input_tokens"], 1200)  # 1000 + 200
        self.assertEqual(row["turn_count"], 7)  # 5 + 2

    def test_insert_turns(self):
        turns = [{
            "session_id": "s1", "timestamp": "2026-04-08T10:00:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 100,
            "output_tokens": 50, "cache_read_tokens": 10,
            "cache_creation_tokens": 5, "tool_name": "Read", "cwd": "/tmp",
        }]
        insert_turns(self.conn, turns)
        rows = self.conn.execute("SELECT * FROM turns").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-sonnet-4-6")


class TestScanIntegration(unittest.TestCase):
    """Integration test: create fake JSONL files and run scan()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        self.projects_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def _write_project_jsonl(self, project_name, session_id, num_turns=3):
        project_dir = self.projects_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        with open(path, "w") as f:
            f.write(_make_user_record(session_id=session_id) + "\n")
            for i in range(num_turns):
                ts = f"2026-04-08T10:{i:02d}:00Z"
                f.write(_make_assistant_record(
                    session_id=session_id,
                    timestamp=ts,
                    input_tokens=100 * (i + 1),
                    output_tokens=50 * (i + 1),
                ) + "\n")

    def test_scan_new_files(self):
        self._write_project_jsonl("user/myproject", "sess-1", num_turns=3)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["sessions"], 1)

    def test_scan_is_incremental(self):
        self._write_project_jsonl("user/myproject", "sess-1")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        # Second scan should skip
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["new"], 0)

    def test_scan_empty_directory(self):
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["turns"], 0)

    def test_scan_multiple_files(self):
        self._write_project_jsonl("user/project-a", "sess-1", num_turns=2)
        self._write_project_jsonl("user/project-b", "sess-2", num_turns=4)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 2)
        self.assertEqual(result["turns"], 6)
        self.assertEqual(result["sessions"], 2)


class TestScanIncrementalUpdate(unittest.TestCase):
    """Test that updating a file only processes new lines (no double reads)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.filepath = self.projects_dir / "sess-1.jsonl"

    def _write_initial(self):
        with open(self.filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1",
                                      timestamp="2026-04-08T09:00:00Z") + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:01:00Z",
                                           input_tokens=100, output_tokens=50) + "\n")

    def _append_turns(self):
        # Ensure mtime visibly changes (filesystem resolution can be ~10ms)
        import time
        time.sleep(0.05)
        with open(self.filepath, "a") as f:
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:05:00Z",
                                           input_tokens=200, output_tokens=100) + "\n")
            f.write(_make_assistant_record(session_id="sess-1",
                                           timestamp="2026-04-08T09:10:00Z",
                                           input_tokens=300, output_tokens=150) + "\n")

    def test_no_duplicate_turns_on_update(self):
        """Growing a file must add only new turns, not re-insert old ones."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        result = scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["turns"], 2)  # only the 2 new turns

        conn = sqlite3.connect(self.db_path)
        total_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        self.assertEqual(total_turns, 3)  # 1 original + 2 new

    def test_token_counts_accumulate_correctly(self):
        """Session totals should reflect all turns, not double-count."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        conn.close()
        # 100 + 200 + 300 = 600
        self.assertEqual(session["total_input_tokens"], 600)
        # 50 + 100 + 150 = 300
        self.assertEqual(session["total_output_tokens"], 300)
        self.assertEqual(session["turn_count"], 3)

    def test_session_timestamp_updated(self):
        """Last timestamp should advance after file grows."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        self._append_turns()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        conn.close()
        self.assertEqual(session["last_timestamp"], "2026-04-08T09:10:00Z")

    def test_new_session_first_timestamp_uses_earliest(self):
        """A brand-new session discovered during an incremental scan with
        non-monotonic timestamps must record the EARLIEST timestamp as
        first_timestamp. Regression for the incremental branch only tracking
        last_timestamp (parse_jsonl_file already tracked both)."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        # Append two records for a NEW session 'sess-2' with timestamps in
        # reverse order — later one observed first, earlier one second.
        import time
        time.sleep(0.05)
        with open(self.filepath, "a") as f:
            f.write(_make_assistant_record(session_id="sess-2",
                                           timestamp="2026-04-08T10:05:00Z",
                                           input_tokens=50, output_tokens=25,
                                           message_id="msg-new-2-late") + "\n")
            f.write(_make_assistant_record(session_id="sess-2",
                                           timestamp="2026-04-08T09:55:00Z",
                                           input_tokens=70, output_tokens=35,
                                           message_id="msg-new-2-early") + "\n")
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        sess2 = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-2'").fetchone()
        conn.close()
        self.assertEqual(sess2["first_timestamp"], "2026-04-08T09:55:00Z")
        self.assertEqual(sess2["last_timestamp"],  "2026-04-08T10:05:00Z")

    def test_tool_calls_recorded_on_incremental_scan(self):
        """tool_calls must be populated for turns ingested incrementally, not
        only on a file's first (full) parse. Regression for the duplicated
        incremental parse loop dropping the tool_calls field: it silently
        NULLed tool-call detail for every turn appended after the first scan."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        # Append a turn carrying tool_use blocks. The shared helper emits empty
        # content, so build this record explicitly.
        import time
        time.sleep(0.05)
        tool_turn = json.dumps({
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": "2026-04-08T09:15:00Z",
            "cwd": "/home/user/project",
            "message": {
                "id": "msg-tool-1",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 120, "output_tokens": 60},
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        })
        with open(self.filepath, "a") as f:
            f.write(tool_turn + "\n")

        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT tool_name, tool_calls FROM turns WHERE message_id = 'msg-tool-1'"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "incrementally-scanned tool turn was not stored")
        self.assertEqual(row["tool_name"], "Edit")  # first tool call
        self.assertIsNotNone(row["tool_calls"], "tool_calls was NULL on incremental scan")
        calls = json.loads(row["tool_calls"])
        self.assertEqual([c["name"] for c in calls], ["Edit", "Bash"])
        self.assertEqual(calls[0]["input"], {"file_path": "a.py", "input_size": 21})

    def test_mtime_change_without_growth_skipped(self):
        """If mtime changes but line count doesn't grow, skip the file."""
        self._write_initial()
        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        # Touch the file (change mtime) without adding content
        import time
        time.sleep(0.05)
        os.utime(self.filepath, None)

        result = scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["turns"], 0)


class TestCrossFileSessionTotals(unittest.TestCase):
    """Test that session totals are correct when the same session spans multiple files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"

    def test_session_across_files_not_inflated(self):
        """Same session in 2 files with duplicate message_ids should not inflate totals."""
        # File 1: message msg-1 with 100 input
        f1 = self.projects_dir / "file1.jsonl"
        with open(f1, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-1",
                                           input_tokens=100, output_tokens=50,
                                           cache_read=0, cache_creation=0) + "\n")

        # File 2: same message msg-1 (duplicate) + new message msg-2
        f2 = self.projects_dir / "file2.jsonl"
        with open(f2, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-1",
                                           input_tokens=100, output_tokens=50,
                                           cache_read=0, cache_creation=0) + "\n")
            f.write(_make_assistant_record(session_id="sess-1", message_id="msg-2",
                                           input_tokens=200, output_tokens=100,
                                           cache_read=0, cache_creation=0) + "\n")

        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Turns table should have 2 turns (msg-1 deduped across files)
        turns = conn.execute("SELECT COUNT(*) as c FROM turns").fetchone()["c"]
        self.assertEqual(turns, 2)

        # Session totals should match turns table, not be inflated
        session = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-1'").fetchone()
        self.assertEqual(session["total_input_tokens"], 300)  # 100 + 200
        self.assertEqual(session["total_output_tokens"], 150)  # 50 + 100
        self.assertEqual(session["turn_count"], 2)
        conn.close()


class TestParseJsonlFileLineCount(unittest.TestCase):
    """Test that parse_jsonl_file returns correct line count."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_line_count_matches_file(self):
        path = os.path.join(self.tmpdir, "test.jsonl")
        with open(path, "w") as f:
            f.write(_make_user_record() + "\n")
            f.write(_make_assistant_record() + "\n")
            f.write(_make_assistant_record(timestamp="2026-04-08T10:01:00Z") + "\n")
        _, _, line_count = parse_jsonl_file(path)
        self.assertEqual(line_count, 3)

    def test_empty_file_returns_zero(self):
        path = os.path.join(self.tmpdir, "empty.jsonl")
        with open(path, "w") as f:
            pass
        _, _, line_count = parse_jsonl_file(path)
        self.assertEqual(line_count, 0)

    def test_partial_trailing_line_is_reread_next_scan(self):
        # Scan lands while the final record is half-written: it must not be
        # counted, or the incremental rescan skips it and the turn keeps its
        # mid-stream output tally forever.
        path = os.path.join(self.tmpdir, "partial.jsonl")
        early = _make_assistant_record(message_id="m1", output_tokens=2)
        final = _make_assistant_record(message_id="m1", output_tokens=1775)
        with open(path, "w") as f:
            f.write(early + "\n" + final[:40])
        _, turns, n = parse_jsonl_file(path)
        self.assertEqual(n, 1)
        self.assertEqual([t["output_tokens"] for t in turns], [2])
        with open(path, "w") as f:
            f.write(early + "\n" + final + "\n")
        _, turns, n = parse_jsonl_file(path, start_line=n)
        self.assertEqual(n, 2)
        self.assertEqual([t["output_tokens"] for t in turns], [1775])

    def test_complete_trailing_line_without_newline_counts(self):
        path = os.path.join(self.tmpdir, "nonl.jsonl")
        with open(path, "w") as f:
            f.write(_make_user_record() + "\n" + _make_assistant_record())
        _, turns, n = parse_jsonl_file(path)
        self.assertEqual((n, len(turns)), (2, 1))


def _make_title_record(session_id="sess-1", rtype="ai-title", title="A title",
                       timestamp="2026-04-08T10:05:00Z", cwd="/home/user/project"):
    record = {
        "type": rtype,
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    }
    record["customTitle" if rtype == "custom-title" else "aiTitle"] = title
    return json.dumps(record)


class TestSessionTopics(unittest.TestCase):
    """custom-title / ai-title parsing, phantom-row prevention, and the
    one-time topic backfill (ported from upstream claude-usage)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_custom_title_wins_over_ai_title(self):
        path = self._write_jsonl("test.jsonl", [
            _make_title_record(rtype="ai-title", title="AI guess"),
            _make_title_record(rtype="custom-title", title="User pick"),
        ])
        metas, turns, _ = parse_jsonl_file(path)
        self.assertEqual(len(turns), 0)
        self.assertEqual(metas[0]["topic"], "User pick")

    def test_custom_title_wins_regardless_of_order(self):
        # custom-title must win even if it's seen BEFORE the ai-title in the file.
        path = self._write_jsonl("test.jsonl", [
            _make_title_record(rtype="custom-title", title="User pick"),
            _make_title_record(rtype="ai-title", title="AI guess"),
        ])
        metas, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "User pick")

    def test_ai_title_used_when_no_custom_title(self):
        path = self._write_jsonl("test.jsonl", [
            _make_title_record(rtype="ai-title", title="AI guess"),
        ])
        metas, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["topic"], "AI guess")

    def test_title_only_record_creates_no_phantom_session_row(self):
        """A transcript containing only a title record (no turns) must not
        create a token-less session row in the DB."""
        projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        projects_dir.mkdir(parents=True)
        db_path = Path(self.tmpdir) / "usage.db"
        with open(projects_dir / "sess-1.jsonl", "w") as f:
            f.write(_make_title_record(session_id="sess-1", rtype="ai-title",
                                       title="Orphan title") + "\n")

        scan(projects_dir=projects_dir.parent.parent, db_path=db_path, verbose=False)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)

    def test_title_after_turns_picked_up_on_incremental_scan(self):
        """Claude Code can append the title record after the turns; an
        incremental scan (which only re-reads new lines) must still pick it up."""
        projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        projects_dir.mkdir(parents=True)
        db_path = Path(self.tmpdir) / "usage.db"
        filepath = projects_dir / "sess-1.jsonl"

        with open(filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1") + "\n")
        scan(projects_dir=projects_dir.parent.parent, db_path=db_path, verbose=False)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        topic_before = conn.execute(
            "SELECT topic FROM sessions WHERE session_id = 'sess-1'").fetchone()["topic"]
        conn.close()
        self.assertIsNone(topic_before)

        self._append_line(filepath, _make_title_record(
            session_id="sess-1", rtype="custom-title", title="Late title"))
        scan(projects_dir=projects_dir.parent.parent, db_path=db_path, verbose=False)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT topic, total_input_tokens FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["topic"], "Late title")
        # Token totals must be unaffected by the title-only append.
        self.assertEqual(row["total_input_tokens"], 100)

    @staticmethod
    def _append_line(filepath, line):
        """Append a line and push mtime 1 s past its pre-append value.

        The incremental scan skips a file whose mtime is within 10 ms of the
        last scan, and back-to-back appends land ~1 ms apart on Windows. An
        explicit, strictly increasing mtime (based on the pre-append value, not
        the wall-clock one the write leaves behind) replaces sleeping."""
        before = os.path.getmtime(filepath)
        with open(filepath, "a") as f:
            f.write(line + "\n")
        os.utime(filepath, (before + 1.0, before + 1.0))

    def _scan_with_appended_title(self, filepath, projects_dir, db_path, rtype, title):
        self._append_line(filepath, _make_title_record(
            session_id="sess-1", rtype=rtype, title=title))
        scan(projects_dir=projects_dir.parent.parent, db_path=db_path, verbose=False)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT topic, topic_source FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        conn.close()
        return row

    def _fresh_session(self):
        """A session with real turns already scanned once, ready for incrementally
        appended title records (mirrors test_title_after_turns_picked_up_...)."""
        projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        projects_dir.mkdir(parents=True)
        db_path = Path(self.tmpdir) / "usage.db"
        filepath = projects_dir / "sess-1.jsonl"
        with open(filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1") + "\n")
        scan(projects_dir=projects_dir.parent.parent, db_path=db_path, verbose=False)
        return filepath, projects_dir, db_path

    def test_ai_then_custom_custom_wins(self):
        filepath, projects_dir, db_path = self._fresh_session()
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "ai-title", "AI guess")
        self.assertEqual((row["topic"], row["topic_source"]), ("AI guess", "ai"))
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "custom-title", "User pick")
        self.assertEqual((row["topic"], row["topic_source"]), ("User pick", "custom"))

    def test_custom_then_ai_custom_survives(self):
        filepath, projects_dir, db_path = self._fresh_session()
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "custom-title", "User pick")
        self.assertEqual((row["topic"], row["topic_source"]), ("User pick", "custom"))
        # A later ai-title must NOT clobber the user's custom title.
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "ai-title", "AI guess")
        self.assertEqual((row["topic"], row["topic_source"]), ("User pick", "custom"))

    def test_ai_then_ai_updates(self):
        filepath, projects_dir, db_path = self._fresh_session()
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "ai-title", "Guess 1")
        self.assertEqual((row["topic"], row["topic_source"]), ("Guess 1", "ai"))
        row = self._scan_with_appended_title(filepath, projects_dir, db_path, "ai-title", "Guess 2")
        self.assertEqual((row["topic"], row["topic_source"]), ("Guess 2", "ai"))


class TestTitleRecordPrecedesProject(unittest.TestCase):
    """A title record with no cwd can create the session's placeholder row before
    any real record is seen; the first real record must still supply the true
    project_name rather than leaving it stuck at 'unknown'."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_first_real_record_replaces_placeholder_project(self):
        path = os.path.join(self.tmpdir, "test.jsonl")
        with open(path, "w") as f:
            f.write(_make_title_record(session_id="sess-1", rtype="ai-title",
                                       title="Some title") + "\n")
            f.write(_make_user_record(session_id="sess-1", cwd="/home/user/real-project") + "\n")
        metas, _, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["project_name"], "user/real-project")


class TestTopicBackfill(unittest.TestCase):
    """One-time backfill of topics for sessions scanned before topic support
    existed, gated by the schema_meta 'topic_backfill_done' marker."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _seed_pre_existing_session(self):
        self.conn.execute("""
            INSERT INTO sessions (session_id, project_name, first_timestamp,
                last_timestamp, model, turn_count, topic)
            VALUES ('sess-1', 'user/proj', '2026-04-08T09:00:00Z',
                '2026-04-08T09:01:00Z', 'claude-sonnet-4-6', 1, NULL)
        """)
        self.conn.commit()

    def test_backfill_fills_topic_from_already_scanned_transcript(self):
        self._seed_pre_existing_session()
        path = os.path.join(self.tmpdir, "old.jsonl")
        with open(path, "w") as f:
            f.write(_make_title_record(session_id="sess-1", rtype="ai-title",
                                       title="Backfilled title") + "\n")

        filled = backfill_topics_once(self.conn, [path], verbose=False)
        self.assertEqual(filled, 1)

        topic = self.conn.execute(
            "SELECT topic FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()["topic"]
        self.assertEqual(topic, "Backfilled title")

    def test_backfill_runs_at_most_once(self):
        self._seed_pre_existing_session()
        path = os.path.join(self.tmpdir, "old.jsonl")
        with open(path, "w") as f:
            f.write(_make_title_record(session_id="sess-1", rtype="ai-title",
                                       title="First run title") + "\n")

        first = backfill_topics_once(self.conn, [path], verbose=False)
        self.assertEqual(first, 1)

        # Clear the topic back to NULL and change the title in the transcript;
        # a second call must be a no-op (marker already set), not refill it.
        self.conn.execute("UPDATE sessions SET topic = NULL WHERE session_id = 'sess-1'")
        self.conn.commit()
        with open(path, "w") as f:
            f.write(_make_title_record(session_id="sess-1", rtype="ai-title",
                                       title="Second run title") + "\n")

        second = backfill_topics_once(self.conn, [path], verbose=False)
        self.assertEqual(second, 0)
        topic = self.conn.execute(
            "SELECT topic FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()["topic"]
        self.assertIsNone(topic)

    def test_missing_dirs_skips_backfill_and_leaves_marker_unset(self):
        # dirs_found=False (e.g. ~/.claude/projects doesn't exist) must not set
        # the marker, so a later scan retries once the directory appears.
        self._seed_pre_existing_session()
        result = backfill_topics_once(self.conn, [], verbose=False, dirs_found=False)
        self.assertEqual(result, 0)
        self.assertIsNone(scanner._meta_get(self.conn, "topic_backfill_done"))
        topic = self.conn.execute(
            "SELECT topic FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()["topic"]
        self.assertIsNone(topic)


class TestEffortBackfill(unittest.TestCase):
    """turns.effort for rows ingested before the column existed. A plain rescan
    can never fix them (INSERT OR IGNORE leaves existing rows alone), so
    ClaudeSource.backfill_effort rewrites them once, gated by a schema_meta
    marker. End-to-end through scan_all, which is how it actually runs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.projects = Path(self.tmpdir) / "projects"
        self.projects.mkdir()
        self.transcript = self.projects / "old.jsonl"
        self.transcript.write_text(
            _make_assistant_record(message_id="msg-old", effort="high") + "\n",
            encoding="utf-8")
        # Seed the turn the way a pre-column scan left it: no effort recorded.
        conn = get_db(self.db_path)
        init_db(conn)
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-sonnet-4-6',
                    100, 50, 10, 5, NULL, '/tmp', 'msg-old')
        """)
        # Mark the transcript as already processed so the incremental walk skips
        # it — only the backfill can reach this row.
        conn.execute(
            "INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, ?, ?, 'claude')",
            (str(self.transcript), os.path.getmtime(self.transcript), 1))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _effort(self):
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT effort FROM turns WHERE message_id = 'msg-old'").fetchone()
        conn.close()
        return row["effort"]

    def _scan(self):
        from sources import ClaudeSource, scan_all
        scan_all(db_path=self.db_path, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def test_scan_backfills_effort_on_an_already_processed_file(self):
        self.assertIn(self._effort(), (None, ""))
        self._scan()
        self.assertEqual(self._effort(), "high")

    def test_backfill_runs_at_most_once(self):
        self._scan()
        # Clear it and change the transcript: the marker must block a second pass.
        conn = get_db(self.db_path)
        conn.execute("UPDATE turns SET effort = '' WHERE message_id = 'msg-old'")
        conn.commit()
        conn.close()
        self.transcript.write_text(
            _make_assistant_record(message_id="msg-old", effort="low") + "\n",
            encoding="utf-8")
        self._scan()
        self.assertEqual(self._effort(), "")

    def test_backfill_only_fills_blanks(self):
        # Direct unit call: the UPDATE must never overwrite a turn that already
        # has an effort, so re-running it can only add data, never rewrite it.
        conn = get_db(self.db_path)
        conn.execute("UPDATE turns SET effort = 'medium' WHERE message_id = 'msg-old'")
        conn.commit()
        filled = scanner.backfill_effort_once(
            conn, "some-other-marker", lambda: [("msg-old", "high")], verbose=False)
        self.assertEqual(filled, 0)
        self.assertEqual(
            conn.execute("SELECT effort FROM turns WHERE message_id = 'msg-old'"
                         ).fetchone()["effort"], "medium")
        conn.close()

    def test_nothing_to_fill_skips_the_corpus_read(self):
        # Fresh-DB path: no row is missing an effort, so pairs_fn (a full
        # transcript read) must never be called — just mark done and return.
        conn = get_db(self.db_path)
        conn.execute("UPDATE turns SET effort = 'high'")
        conn.commit()
        calls = []

        def pairs():
            calls.append(1)
            return []

        filled = scanner.backfill_effort_once(conn, "fresh-marker", pairs, verbose=False)
        self.assertEqual(filled, 0)
        self.assertEqual(calls, [], "pairs_fn was called despite nothing to fill")
        self.assertEqual(scanner._meta_get(conn, "fresh-marker"), "1")
        conn.close()

    def test_backfill_update_uses_the_message_id_index(self):
        # idx_turns_message_id is PARTIAL, so a query that doesn't restate its
        # predicate silently falls back to a full table scan — one per turn, which
        # took 13 minutes over a real corpus instead of 3 seconds. The plan is the
        # only visible symptom (the result is identical either way), so assert it.
        conn = get_db(self.db_path)
        plan = " ".join(str(r[-1]) for r in conn.execute(
            "EXPLAIN QUERY PLAN " + scanner.BACKFILL_EFFORT_SQL, ("high", "msg-old")))
        conn.close()
        self.assertIn("idx_turns_message_id", plan, f"full scan? plan was: {plan}")

    def test_missing_dir_leaves_marker_unset(self):
        from sources import ClaudeSource, scan_all
        missing = str(Path(self.tmpdir) / "gone")
        scan_all(db_path=self.db_path, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[missing])])
        conn = get_db(self.db_path)
        self.assertIsNone(scanner._meta_get(conn, "effort_backfill_done_claude"))
        conn.close()


class TestTopicBackfillWiredThroughSources(unittest.TestCase):
    """The backfill hook lives on ClaudeSource (sources/claude.py) and is invoked
    by sources.scan_all before the per-source walk — CodexSource has no such
    method, so hasattr() skips it without error (Codex sessions simply keep
    topic NULL). This exercises the hook end-to-end through the real seam, on
    top of the direct backfill_topics_once() unit tests above."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.codex_dir = Path(self.tmpdir) / "codex"
        self.codex_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def test_scan_all_runs_backfill_hook_and_sets_marker(self):
        import sources
        import sources.codex as codex_mod

        filepath = self.projects_dir / "sess-1.jsonl"
        with open(filepath, "w") as f:
            f.write(_make_user_record(session_id="sess-1") + "\n")
            f.write(_make_assistant_record(session_id="sess-1") + "\n")
            f.write(_make_title_record(session_id="sess-1", rtype="custom-title",
                                       title="Seam title") + "\n")

        import config
        original_codex_path, original_blob = codex_mod.CODEX_PATH, config.DESKTOP_BLOB_DIR
        codex_mod.CODEX_PATH = self.codex_dir
        config.DESKTOP_BLOB_DIR = self.codex_dir / "no-desktop-cache"  # never the real cache
        try:
            stats = sources.scan_all(db_path=self.db_path,
                                     projects_dir=self.projects_dir,
                                     verbose=False)
        finally:
            codex_mod.CODEX_PATH, config.DESKTOP_BLOB_DIR = original_codex_path, original_blob

        # Both sources ran (CodexSource, which lacks backfill_topics, didn't crash).
        self.assertIn("claude", stats)
        self.assertIn("codex", stats)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # The title record is in this same (first-ever) scan, so it's picked up by
        # the normal per-source parse, not the backfill (nothing pre-existed for
        # the backfill to find) — see TestTopicBackfill above for that path.
        topic = conn.execute(
            "SELECT topic FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()["topic"]
        # The hook still ran (and found nothing to do), so the marker is set —
        # proving the seam wiring itself, independent of what it found.
        marker = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'topic_backfill_done'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["value"], "1")

    def test_missing_projects_dir_leaves_marker_unset(self):
        # ClaudeSource pointed at a nonexistent projects dir must not set the
        # backfill-done marker (it never actually scanned a real transcript dir).
        import sources.claude as claude_mod

        missing_dir = Path(self.tmpdir) / "does-not-exist"
        conn = get_db(self.db_path)
        init_db(conn)
        src = claude_mod.ClaudeSource(projects_dirs=[missing_dir])
        filled = src.backfill_topics(conn)
        self.assertEqual(filled, 0)
        marker = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'topic_backfill_done'"
        ).fetchone()
        conn.close()
        self.assertIsNone(marker)


if __name__ == "__main__":
    unittest.main()


class TestSubagentTypeSweep(unittest.TestCase):
    """ClaudeSource.sync_agents fills agents.agent_type from each subagent's own
    .meta.json, for the case that leaves roughly half a real corpus untyped: the
    parent's tool_result closes the dispatch without ever naming an agentType.

    The second scan is the point of this test. sync_agents writes through
    upsert_agents, whose ON CONFLICT clause re-runs on every later scan that sees
    the same typeless tool_result — without the COALESCE guard the sweep's value is
    overwritten with NULL and the fix silently undoes itself.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.projects = Path(self.tmpdir) / "projects"
        sub = self.projects / "sess-1" / "subagents"
        sub.mkdir(parents=True)
        # Parent transcript: one ordinary turn plus a dispatch record that knows the
        # agent id but NOT its type (agent_type=None -> key omitted downstream).
        (self.projects / "sess-1.jsonl").write_text(
            _make_assistant_record(message_id="msg-parent") + "\n"
            + _dispatch_record(agent_id="ag-typeless", agent_type=None) + "\n",
            encoding="utf-8")
        # The subagent's own transcript + the meta sidecar Claude Code writes next
        # to it. The agent id lives in the FILENAME, not in the meta payload.
        # Every line of a real subagent transcript carries the agentId that ties
        # its turns back to the agents row.
        sub_record = json.loads(
            _make_assistant_record(message_id="msg-sub", is_sidechain=True))
        sub_record["agentId"] = "ag-typeless"
        (sub / "agent-ag-typeless.jsonl").write_text(
            json.dumps(sub_record) + "\n", encoding="utf-8")
        (sub / "agent-ag-typeless.meta.json").write_text(
            json.dumps({"agentType": "task-implementer",
                        "description": "Implement the thing",
                        "toolUseId": "toolu_1", "spawnDepth": 1, "model": "sonnet"}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        from sources import ClaudeSource, scan_all
        scan_all(db_path=self.db_path, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def _agent_type(self):
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT agent_type FROM agents WHERE agent_id = 'ag-typeless'").fetchone()
        conn.close()
        return row["agent_type"] if row else None

    def test_sweep_types_an_agent_the_dispatch_record_never_named(self):
        self._scan()
        self.assertEqual(self._agent_type(), "task-implementer")

    def test_type_survives_a_re_parse_of_the_parent(self):
        # A plain second scan skips the unchanged parent and so never re-upserts
        # the typeless dispatch record — it cannot see this regression. The path
        # that can is a forced re-parse (scanner.force_reparse_once, used by every
        # one-time backfill), which clears processed_files and re-reads the parent
        # from line 0. Without the COALESCE in upsert_agents, that second read
        # writes NULL over the swept type and the fix silently undoes itself.
        self._scan()
        conn = get_db(self.db_path)
        conn.execute("DELETE FROM processed_files")
        conn.commit()
        conn.close()
        self._scan()
        self.assertEqual(self._agent_type(), "task-implementer")

    def test_typed_turns_group_under_their_agent(self):
        # The join the breakdown endpoint does: the subagent's turn must land under
        # its type, not in the main-thread bucket.
        self._scan()
        conn = get_db(self.db_path)
        rows = dict(conn.execute("""
            SELECT COALESCE(a.agent_type, 'main thread'), COUNT(*)
            FROM turns t LEFT JOIN agents a ON a.agent_id = t.agent_id
            WHERE t.message_id NOT LIKE 'agent_summary:%'
            GROUP BY 1
        """).fetchall())
        conn.close()
        self.assertEqual(rows.get("task-implementer"), 1)
        self.assertEqual(rows.get("main thread"), 1)

    def test_sweep_leaves_a_known_type_alone(self):
        # A meta.json must never downgrade a type the dispatch record supplied.
        self._scan()
        conn = get_db(self.db_path)
        conn.execute("UPDATE agents SET agent_type = 'hand-set' WHERE agent_id = 'ag-typeless'")
        conn.commit()
        conn.close()
        self._scan()
        self.assertEqual(self._agent_type(), "hand-set")

    def test_sweep_does_not_wipe_what_the_dispatch_record_knew(self):
        # The two records describe the same agent from different angles: the
        # tool_result carries the run totals (tokens/status/tool count), the
        # meta.json carries the type. The sweep upserts a row with NULLs in every
        # totals column, so without the COALESCE in upsert_agents it erases them.
        self._scan()
        conn = get_db(self.db_path)
        row = conn.execute("""
            SELECT agent_type, status, total_tokens, tool_use_count
            FROM agents WHERE agent_id = 'ag-typeless'
        """).fetchone()
        conn.close()
        self.assertEqual(row["agent_type"], "task-implementer")   # from meta.json
        self.assertEqual(row["status"], "completed")              # from tool_result
        self.assertEqual(row["total_tokens"], 4096)
        self.assertEqual(row["tool_use_count"], 11)


class TestSubagentAttributionFallback(unittest.TestCase):
    """No .meta.json sidecar: sync_agents falls back to `attributionAgent` in the
    subagent's own jsonl (upstream claude-usage's source for the same name).

    Same corpus shape as TestSubagentTypeSweep minus the sidecar, which is the
    case that would otherwise leave the agent NULL-typed and collapsed into the
    main-thread bucket.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.projects = Path(self.tmpdir) / "projects"
        self.sub = self.projects / "sess-1" / "subagents"
        self.sub.mkdir(parents=True)
        (self.projects / "sess-1.jsonl").write_text(
            _make_assistant_record(message_id="msg-parent") + "\n"
            + _dispatch_record(agent_id="ag-nometa", agent_type=None) + "\n",
            encoding="utf-8")
        sub_record = json.loads(
            _make_assistant_record(message_id="msg-sub", is_sidechain=True))
        sub_record["agentId"] = "ag-nometa"
        sub_record["attributionAgent"] = "Explore"
        (self.sub / "agent-ag-nometa.jsonl").write_text(
            json.dumps(sub_record) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        from sources import ClaudeSource, scan_all
        scan_all(db_path=self.db_path, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def _agent_type(self, agent_id="ag-nometa"):
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT agent_type FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        conn.close()
        return row["agent_type"] if row else None

    def test_jsonl_attribution_names_a_sidecarless_agent(self):
        self._scan()
        self.assertEqual(self._agent_type(), "Explore")

    def test_sidecar_still_wins_over_attribution(self):
        # Both present and disagreeing: the meta pass runs first and the second
        # pass must not touch what it named.
        (self.sub / "agent-ag-nometa.meta.json").write_text(
            json.dumps({"agentType": "task-implementer"}), encoding="utf-8")
        self._scan()
        self.assertEqual(self._agent_type(), "task-implementer")

    def test_attribution_does_not_overwrite_a_dispatch_supplied_type(self):
        self._scan()
        conn = get_db(self.db_path)
        conn.execute("UPDATE agents SET agent_type = 'hand-set'")
        conn.commit()
        conn.close()
        self._scan()
        self.assertEqual(self._agent_type(), "hand-set")

    def test_turns_group_under_the_attributed_type(self):
        self._scan()
        conn = get_db(self.db_path)
        rows = dict(conn.execute("""
            SELECT COALESCE(a.agent_type, 'main thread'), COUNT(*)
            FROM turns t LEFT JOIN agents a ON a.agent_id = t.agent_id
            WHERE t.message_id NOT LIKE 'agent_summary:%'
            GROUP BY 1
        """).fetchall())
        conn.close()
        self.assertEqual(rows.get("Explore"), 1)
        self.assertEqual(rows.get("main thread"), 1)

    def test_an_unnamed_subagent_stays_untyped(self):
        # No sidecar and no attributionAgent: nothing to invent, and the sweep
        # must not write a bogus row.
        (self.sub / "agent-ag-blank.jsonl").write_text(
            json.dumps({"type": "assistant", "agentId": "ag-blank"}) + "\n",
            encoding="utf-8")
        self._scan()
        self.assertIsNone(self._agent_type("ag-blank"))


class TestSkillBackfill(unittest.TestCase):
    """turns.skill for rows ingested before the column existed — same one-shot
    UPDATE path as effort, sharing scanner.backfill_column_once."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "usage.db"
        self.projects = Path(self.tmpdir) / "projects"
        self.projects.mkdir()
        record = json.loads(_make_assistant_record(message_id="msg-old"))
        record["attributionSkill"] = "next-task"
        self.transcript = self.projects / "old.jsonl"
        self.transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
        conn = get_db(self.db_path)
        init_db(conn)
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-sonnet-4-6',
                    100, 50, 10, 5, NULL, '/tmp', 'msg-old')
        """)
        conn.execute(
            "INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, ?, ?, 'claude')",
            (str(self.transcript), os.path.getmtime(self.transcript), 1))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _skill(self):
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT skill FROM turns WHERE message_id = 'msg-old'").fetchone()
        conn.close()
        return row["skill"]

    def _scan(self):
        from sources import ClaudeSource, scan_all
        scan_all(db_path=self.db_path, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def test_scan_backfills_skill_on_an_already_processed_file(self):
        self.assertIn(self._skill(), (None, ""))
        self._scan()
        self.assertEqual(self._skill(), "next-task")

    def test_fresh_parse_captures_skill_inline(self):
        # No pre-seeded row: the walk itself must record the skill.
        db2 = Path(self.tmpdir) / "fresh.db"
        from sources import ClaudeSource, scan_all
        scan_all(db_path=db2, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])
        conn = get_db(db2)
        row = conn.execute(
            "SELECT skill FROM turns WHERE message_id = 'msg-old'").fetchone()
        conn.close()
        self.assertEqual(row["skill"], "next-task")

    def test_backfill_uses_the_message_id_index(self):
        # Mirrors the effort guard: the partial index only kicks in when the query
        # restates its predicate, and losing it is a 13-minute full scan, not a
        # visible failure.
        conn = get_db(self.db_path)
        plan = " ".join(str(r[-1]) for r in conn.execute(
            "EXPLAIN QUERY PLAN " + scanner.backfill_column_sql("skill"),
            ("next-task", "msg-old")))
        conn.close()
        self.assertIn("idx_turns_message_id", plan, f"full scan? plan was: {plan}")

    def test_advisor_turns_inherit_their_parent_skill(self):
        # An advisor iteration is minted as a copy of its parent turn, so it must
        # land in the parent's skill bucket. Historical rows can't be reached by a
        # message_id backfill (their id is synthetic), so a second pass copies it
        # down — without it the advisor's tokens, which are the expensive half of an
        # advisor call, pool into "main thread" while the parent sits under a skill.
        record = json.loads(_make_assistant_record(message_id="msg-adv"))
        record["attributionSkill"] = "next-task"
        record["advisorModel"] = "claude-fable-5"
        record["message"]["usage"]["iterations"] = [
            {"type": "message", "input_tokens": 100, "output_tokens": 50},
            {"type": "advisor_message", "input_tokens": 9000, "output_tokens": 400},
        ]
        (self.projects / "adv.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8")
        # Seed the advisor row the way a pre-column scan left it: no skill, and the
        # parent already present so only the propagation pass can fix the child.
        conn = get_db(self.db_path)
        for mid, skill in (("msg-adv", "next-task"), ("advisor:msg-adv:1", "")):
            conn.execute("""
                INSERT INTO turns (session_id, timestamp, model, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    tool_name, cwd, message_id, skill)
                VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-opus-4-8',
                        100, 50, 0, 0, NULL, '/tmp', ?, ?)
            """, (mid, skill))
        conn.commit()
        conn.close()

        self._scan()

        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT skill FROM turns WHERE message_id = 'advisor:msg-adv:1'").fetchone()
        conn.close()
        self.assertEqual(row["skill"], "next-task")


class TestMcpAttribution(unittest.TestCase):
    """turns.mcp_server/mcp_tool/plugin: captured inline on a fresh parse, and
    backfilled once for rows ingested before the columns existed."""

    ATTR = {"attributionMcpServer": "github", "attributionMcpTool": "workitem",
            "attributionPlugin": "web-app"}

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects = Path(self.tmpdir) / "projects"
        self.projects.mkdir()
        record = json.loads(_make_assistant_record(message_id="msg-mcp"))
        record.update(self.ATTR)
        plain = json.loads(_make_assistant_record(message_id="msg-plain"))
        self.transcript = self.projects / "t.jsonl"
        self.transcript.write_text(
            json.dumps(record) + "\n" + json.dumps(plain) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self, db):
        from sources import ClaudeSource, scan_all
        scan_all(db_path=db, verbose=False,
                 sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def _row(self, db, mid):
        conn = get_db(db)
        row = conn.execute("SELECT mcp_server, mcp_tool, plugin FROM turns "
                           "WHERE message_id = ?", (mid,)).fetchone()
        conn.close()
        return tuple(row)

    def test_fresh_parse_captures_inline(self):
        db = Path(self.tmpdir) / "fresh.db"
        self._scan(db)
        self.assertEqual(self._row(db, "msg-mcp"), ("github", "workitem", "web-app"))
        self.assertEqual(self._row(db, "msg-plain"), ("", "", ""))

    def test_backfill_fills_an_already_processed_file(self):
        db = Path(self.tmpdir) / "old.db"
        conn = get_db(db)
        init_db(conn)
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES ('sess-1', '2026-04-08T10:00:00Z', 'claude-sonnet-4-6',
                    100, 50, 10, 5, NULL, '/tmp', 'msg-mcp')
        """)
        conn.execute(
            "INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, ?, ?, 'claude')",
            (str(self.transcript), os.path.getmtime(self.transcript), 2))
        conn.commit()
        conn.close()
        self._scan(db)
        self.assertEqual(self._row(db, "msg-mcp"), ("github", "workitem", "web-app"))

    def test_dashboard_groups_cost_by_server(self):
        import api
        db = Path(self.tmpdir) / "fresh.db"
        self._scan(db)
        rows = api.get_dashboard_data(db_path=db)["mcp_by_model"]
        self.assertEqual([(r["mcp"], r["turns"]) for r in rows], [("github", 1)])


class TestMidStreamTallies(unittest.TestCase):
    """A scan landing while a response streams stores partial tallies; the next
    scan must correct the row, not be ignored by the unique message_id index."""

    def test_next_scan_corrects_a_turn_stored_mid_stream(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            projects = tmp / "projects"; projects.mkdir()
            fp = projects / "s.jsonl"; db = tmp / "u.db"
            fp.write_text(_make_assistant_record(message_id="msg-1", input_tokens=1200,
                                                 output_tokens=3) + "\n", encoding="utf-8")
            os.utime(fp, (1_000_000, 1_000_000))
            from sources import ClaudeSource, scan_all
            run = lambda: scan_all(db_path=db, verbose=False,
                                   sources=[ClaudeSource(projects_dirs=[str(projects)])])
            run()
            with open(fp, "a", encoding="utf-8") as f:
                f.write(_make_assistant_record(message_id="msg-1", input_tokens=1200,
                                               output_tokens=342) + "\n")
            os.utime(fp, (1_000_100, 1_000_100))
            run()
            conn = get_db(db)
            rows = conn.execute("SELECT output_tokens FROM turns").fetchall()
            total = conn.execute("SELECT total_output_tokens FROM sessions").fetchone()[0]
            conn.close()
            self.assertEqual([r[0] for r in rows], [342])
            self.assertEqual(total, 342)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
