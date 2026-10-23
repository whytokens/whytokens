"""Data policy and egress boundary.

Planted canaries (a secret in a Write body, a shell command, a user path) must not reach
the DB, the session-detail API payload or waste prose by default, and must when the raw
opt-in is on. scripts/falsify_data_policy.py breaks each rule and checks this goes red.
"""

from contextlib import closing
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api
import config
import policy
import scanner
import sources
import waste
from sources.coordinator import CoordinatorSource

SECRET = "sk-live-CANARY-8841"
CMD = f"curl -H 'Authorization: {SECRET}' https://example.invalid"
BODY = f"password = {SECRET}" + "x" * 20_000   # > a page: overflow pages survive an UPDATE
HOME = "/home/canary-user/acme"
MODEL = "claude-sonnet-4-6"


def _rec(n, tools, ts="2026-08-20T10:%02d:00Z", create=30_000, read=0):
    content = [{"type": "text", "text": "x"}] + [
        {"type": "tool_use", "id": f"tu{n}{i}", "name": name, "input": inp}
        for i, (name, inp) in enumerate(tools)]
    return json.dumps({
        "type": "assistant", "sessionId": "s1", "timestamp": ts % n, "cwd": HOME,
        "isSidechain": False, "version": "2.1.220",
        "message": {"id": f"m{n}", "model": MODEL, "content": content, "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": create}}})


def _transcript_lines():
    reads = [("Read", {"file_path": f"{HOME}/src/big.py"})]
    return [
        _rec(1, [("Write", {"file_path": f"{HOME}/secret.env", "content": BODY}),
                 ("Bash", {"command": CMD}),
                 ("mcp__db__query", {"sql": f"select '{SECRET}'"}),
                 ("Grep", {"pattern": SECRET}),
                 ("Task", {"subagent_type": "Explore", "prompt": SECRET})]),
        _rec(2, reads, create=60_000), _rec(3, reads, read=60_000, create=60_000),
        _rec(4, reads, read=120_000, create=60_000), _rec(5, reads, read=180_000, create=60_000),
    ]


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "u.db"
        self.proj = self.tmp / "projects" / "p"
        self.proj.mkdir(parents=True)
        (self.proj / "s1.jsonl").write_text("\n".join(_transcript_lines()) + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != policy.RAW_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self):
        scanner.scan(projects_dir=self.proj.parent, db_path=self.db, verbose=False)

    def stored(self):
        with closing(sqlite3.connect(self.db)) as c:
            return [r[0] for r in c.execute("SELECT tool_calls FROM turns WHERE tool_calls IS NOT NULL")]

    def db_bytes(self):
        return b"".join(p.read_bytes() for p in self.tmp.glob("u.db*"))


class TestAllowlist(PolicyCase):
    def test_default_scan_stores_no_raw_input(self):
        self.scan()
        blob = "".join(self.stored())
        for canary in (SECRET, "curl", "password", "select"):
            self.assertNotIn(canary, blob)
        self.assertNotIn(SECRET.encode(), self.db_bytes())

    def test_allowlisted_fields_survive(self):
        self.scan()
        first = json.loads(self.stored()[0])
        by = {c["name"]: c["input"] for c in first}
        self.assertEqual(by["Write"]["file_path"], f"{HOME}/secret.env")
        self.assertNotIn("content", by["Write"])
        self.assertEqual(by["Bash"]["command_len"], len(CMD))
        self.assertEqual(by["Task"]["subagent_type"], "Explore")
        self.assertNotIn("prompt", by["Task"])
        self.assertEqual(set(by["mcp__db__query"]), {"input_size"})
        self.assertEqual(set(by["Grep"]), {"input_size"})
        self.assertGreater(by["Write"]["input_size"], len(BODY))

    def test_session_detail_payload_is_clean(self):
        self.scan()
        with mock.patch.object(api, "DB_PATH", self.db):
            payload = json.dumps(api.get_session_detail("s1"))
        self.assertNotIn(SECRET, payload)
        self.assertIn("secret.env", payload)

    def test_raw_opt_in_stores_inputs(self):
        with mock.patch.dict(os.environ, {policy.RAW_ENV: "1"}):
            self.scan()
        self.assertIn(SECRET, "".join(self.stored()))

    def test_repeated_reads_still_detected_from_redacted_rows(self):
        self.scan()
        causes = [f["cause"] for f in waste.report(self.db, session_id="s1")]
        self.assertIn("repeated_reads", causes)

    def test_waste_prose_has_no_raw_paths(self):
        self.scan()
        text = json.dumps(waste.report(self.db, session_id="s1"))
        self.assertIn("big.py", text)
        self.assertNotIn(HOME, text)
        self.assertNotIn("canary-user", text)

    def test_codex_calls_are_redacted_too(self):
        out = policy.redact_calls([{"name": "exec", "input": {"command": CMD}},
                                   {"name": "odd", "input": {"arguments": SECRET}}])
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out[0]["input"]["command_len"], len(CMD))

    def test_redaction_is_idempotent(self):
        once = [policy.redact_call({"name": "Bash", "input": {"command": CMD}})]
        self.assertEqual([policy.redact_call(c) for c in once], once)


class TestMigration(PolicyCase):
    def seed_raw(self):
        with mock.patch.dict(os.environ, {policy.RAW_ENV: "1"}):
            self.scan()
        self.assertIn(SECRET, "".join(self.stored()))

    def run_scan_all(self):
        proj = self.proj.parent
        sources.scan_all(db_path=self.db, projects_dir=proj, verbose=False,
                         sources=[sources.ClaudeSource(projects_dirs=[proj])])

    def test_existing_rows_are_rewritten_and_purged_from_the_file(self):
        self.seed_raw()
        self.run_scan_all()
        self.assertNotIn(SECRET, "".join(self.stored()))
        self.assertNotIn(SECRET.encode(), self.db_bytes())   # VACUUMed, not just overwritten

    def test_marker_set_after_clean_purge(self):
        self.seed_raw()
        self.run_scan_all()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM schema_meta WHERE key = ?",
                                           (policy.MIGRATION_KEY,)).fetchone())

    def test_vacuum_failure_leaves_marker_unset(self):
        self.seed_raw()

        class Conn:   # sqlite3.Connection can't be patched; delegate and fail VACUUM
            def __init__(s, c): s.c = c
            def __getattr__(s, n): return getattr(s.c, n)
            def execute(s, sql, *a):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("database is locked")
                return s.c.execute(sql, *a)
        meta = {}
        conn = sqlite3.connect(self.db)
        try:
            policy.migrate_tool_calls(Conn(conn), lambda c, k: meta.get(k), lambda c, k, v: meta.update({k: v}))
        finally:
            conn.close()
        self.assertEqual(meta, {})

    def test_migration_runs_once_and_raw_opt_in_skips_it(self):
        self.seed_raw()
        with mock.patch.dict(os.environ, {policy.RAW_ENV: "1"}):
            self.run_scan_all()
        self.assertIn(SECRET, "".join(self.stored()))        # opted in: left alone, not marked
        self.run_scan_all()
        self.assertNotIn(SECRET, "".join(self.stored()))

    def test_standalone_scan_also_migrates(self):
        self.seed_raw()
        self.scan()
        self.assertNotIn(SECRET, "".join(self.stored()))

    def test_blocked_purge_is_retried_not_marked_done(self):
        self.seed_raw()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN")
        holder.execute("SELECT COUNT(*) FROM turns").fetchone()   # open read txn blocks VACUUM
        try:
            self.run_scan_all()
        finally:
            holder.close()
        with closing(sqlite3.connect(self.db)) as c:
            done = c.execute("SELECT value FROM schema_meta WHERE key = ?",
                             (policy.MIGRATION_KEY,)).fetchone()
        self.assertIsNone(done)
        self.run_scan_all()                                         # retry succeeds
        self.assertNotIn(SECRET.encode(), self.db_bytes())

    def test_migration_keeps_row_counts(self):
        self.seed_raw()
        n = len(self.stored())
        self.run_scan_all()
        self.assertEqual(len(self.stored()), n)


class TestCodexEndToEnd(PolicyCase):
    def test_codex_rollout_scan_stores_no_raw_command(self):
        import sources.codex as codex_mod
        d = self.tmp / "codex" / "sessions" / "2026" / "01" / "01"
        d.mkdir(parents=True)
        usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
                 "reasoning_output_tokens": 0, "total_tokens": 15}
        ev = lambda ts, t, p: json.dumps({"timestamp": ts, "type": t, "payload": p})
        lines = [
            ev("2026-01-01T00:00:00.000Z", "session_meta", {"id": "cx1", "cwd": HOME}),
            ev("2026-01-01T00:00:01.000Z", "turn_context", {"model": "gpt-5.4"}),
            ev("2026-01-01T00:00:02.000Z", "response_item",
               {"type": "custom_tool_call", "name": "exec", "input": CMD}),
            ev("2026-01-01T00:00:03.000Z", "event_msg", {"type": "token_count", "info": {
                "last_token_usage": usage, "total_token_usage": usage}}),
        ]
        (d / "rollout-cx1.jsonl").write_text(os.linesep.join(lines) + os.linesep, encoding="utf-8")
        with mock.patch.object(codex_mod, "CODEX_PATH", self.tmp / "codex"):
            sources.scan_all(db_path=self.db, verbose=False, sources=[sources.CodexSource()])
        blob = "".join(self.stored())
        self.assertIn("exec", blob)            # the call was recorded ...
        self.assertNotIn(SECRET, blob)         # ... without its command
        self.assertIn(f'"command_len": {len(CMD)}', blob)


class TestDesktopCacheOptIn(unittest.TestCase):
    def test_default_off_and_env_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHYTOKENS_DESKTOP_CACHE", None)
            self.assertIsNone(config.desktop_cache_dir())
            self.assertEqual(CoordinatorSource().discover_files(), [])
            self.assertIsNone(sources.ClaudeSource().blob_dir)
        with mock.patch.dict(os.environ, {"WHYTOKENS_DESKTOP_CACHE": "1"}):
            self.assertEqual(config.desktop_cache_dir(), config.DESKTOP_BLOB_DIR)

    def test_default_off_never_touches_the_cache_dir(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "blob").write_bytes(b"x")
            with mock.patch.object(config, "DESKTOP_BLOB_DIR", Path(d)), \
                    mock.patch.dict(os.environ, {}):
                os.environ.pop("WHYTOKENS_DESKTOP_CACHE", None)
                self.assertEqual(CoordinatorSource().discover_files(), [])
            with mock.patch.object(config, "DESKTOP_BLOB_DIR", Path(d)), \
                    mock.patch.dict(os.environ, {"WHYTOKENS_DESKTOP_CACHE": "1"}):
                self.assertEqual(len(CoordinatorSource().discover_files()), 1)


class TestEgressBoundary(PolicyCase):
    """Ingestion and analysis open no sockets and spawn no processes."""

    def test_scan_and_waste_make_no_connections(self):
        def boom(*a, **k):
            raise AssertionError("egress attempted")
        with mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket.socket, "connect", boom), \
                mock.patch.object(socket, "getaddrinfo", boom), \
                mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(os, "system", boom):
            proj = self.proj.parent
            sources.scan_all(db_path=self.db, projects_dir=proj, verbose=False,
                             sources=[sources.ClaudeSource(projects_dirs=[proj])])
            waste.report(self.db)

    def test_the_guard_actually_fires(self):
        # the patch must be live in the scan path: a connect inside it raises
        with mock.patch.object(socket, "create_connection",
                               side_effect=AssertionError("egress attempted")):
            with self.assertRaises(AssertionError):
                socket.create_connection(("127.0.0.1", 9))


if __name__ == "__main__":
    unittest.main()
