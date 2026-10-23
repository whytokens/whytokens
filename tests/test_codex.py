"""Tests for the Codex ingestion source (sources/codex.py) + its drift armor.

Mirrors test_schema.py's jobs for the Codex envelope:
  * Golden extraction — the committed Codex fixture parses to the exact normalised
    tokens / models / flags we expect (regression guard for the parser).
  * Token normalisation — Codex's cached-included input is mapped into the Claude
    column convention (input excludes cache; cache_read holds cached).
  * Delta recovery — cumulative-only token_count records recover their per-turn delta.
  * Dedup / idempotency — synthetic ids collapse branch copies and make rescans a
    no-op, end-to-end through the multi-source engine.
  * Schema canary + doctor — the fixture stays inside the Codex allowlists, and
    drift is actually caught.
"""

import json
import os
import time
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import schema
import sources
from sources.codex import parse_file, CodexSource, _synth_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_CODEX = FIXTURES / "codex" / "golden-codex.jsonl"


def _load_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _make_codex_home(files):
    """Build a temp CODEX_HOME with sessions/<name> rollout files from {name: src}."""
    home = tempfile.mkdtemp()
    sess = Path(home) / "sessions" / "2026" / "01" / "01"
    sess.mkdir(parents=True)
    for name, src in files.items():
        shutil.copy(src, sess / name)
    return home


class TestCodexExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metas, cls.turns, cls.line_count = parse_file(str(GOLDEN_CODEX))

    def test_line_count(self):
        self.assertEqual(self.line_count, 13)

    def test_only_token_bearing_records_become_turns(self):
        # 3 token_count records carry usage; the null-info one, the response_item
        # message, the turn_contexts, session_meta, and compacted do not.
        self.assertEqual(len(self.turns), 3)

    def test_single_session(self):
        self.assertEqual([m["session_id"] for m in self.metas], ["codex-golden-1"])

    def test_project_from_cwd(self):
        self.assertEqual(self.metas[0]["project_name"], "acme/api")

    def test_token_normalisation_first_turn(self):
        # last={in:1000, cached:200} -> fresh input 800, cache_read 200
        t = self.turns[0]
        self.assertEqual(t["input_tokens"], 800)
        self.assertEqual(t["cache_read_tokens"], 200)
        self.assertEqual(t["output_tokens"], 50)
        self.assertEqual(t["reasoning_tokens"], 10)
        self.assertEqual(t["cache_creation_tokens"], 0)
        self.assertEqual(t["model"], "gpt-5.5")
        self.assertEqual(t["agent"], "codex")

    def test_per_turn_model_switch(self):
        # The third turn follows a turn_context that switched the model.
        self.assertEqual(self.turns[2]["model"], "gpt-5.3-codex")
        self.assertEqual(self.turns[2]["input_tokens"], 500)
        self.assertEqual(self.turns[2]["cache_read_tokens"], 0)

    def test_effort_tracked_statefully(self):
        # turn_context t1 sets effort=high (turns 0-1); t2 switches to medium,
        # and the later thread_settings_applied re-announces medium.
        self.assertEqual([t["effort"] for t in self.turns],
                         ["high", "high", "medium"])

    def test_session_totals(self):
        ti = sum(t["input_tokens"] for t in self.turns)
        tcr = sum(t["cache_read_tokens"] for t in self.turns)
        to = sum(t["output_tokens"] for t in self.turns)
        tr = sum(t["reasoning_tokens"] for t in self.turns)
        self.assertEqual((ti, tcr, to, tr), (1800, 1700, 330, 130))

    def test_synthetic_ids_unique_and_prefixed(self):
        ids = [t["message_id"] for t in self.turns]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(i.startswith("codex:") for i in ids))

    def test_null_info_skipped(self):
        # No turn was produced for the info:null record (would have model gpt-5.5
        # but zero tokens); all 3 turns carry real usage.
        self.assertTrue(all(t["input_tokens"] + t["output_tokens"] > 0 for t in self.turns))


class TestCodexDeltaRecovery(unittest.TestCase):
    """token_count with only cumulative total_token_usage -> recover per-turn delta."""

    def test_cumulative_only_records(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "rollout-x.jsonl")
        recs = [
            {"timestamp": "2026-05-01T10:00:00Z", "type": "session_meta",
             "payload": {"id": "s", "cwd": "/p/q", "cli_version": "0.130.0"}},
            {"timestamp": "2026-05-01T10:00:01Z", "type": "turn_context",
             "payload": {"model": "gpt-5.5"}},
            {"timestamp": "2026-05-01T10:00:05Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {
                 "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100,
                                       "output_tokens": 50, "reasoning_output_tokens": 5}}}},
            {"timestamp": "2026-05-01T10:00:10Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {
                 "total_token_usage": {"input_tokens": 3000, "cached_input_tokens": 600,
                                       "output_tokens": 130, "reasoning_output_tokens": 25}}}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        _, turns, _ = parse_file(path)
        self.assertEqual(len(turns), 2)
        # delta 1: in 1000, cached 100 -> fresh 900, cr 100, out 50, reason 5
        self.assertEqual((turns[0]["input_tokens"], turns[0]["cache_read_tokens"],
                          turns[0]["output_tokens"], turns[0]["reasoning_tokens"]),
                         (900, 100, 50, 5))
        # delta 2: total in 3000-1000=2000, cached 600-100=500 -> fresh 1500, cr 500,
        # out 130-50=80, reason 25-5=20
        self.assertEqual((turns[1]["input_tokens"], turns[1]["cache_read_tokens"],
                          turns[1]["output_tokens"], turns[1]["reasoning_tokens"]),
                         (1500, 500, 80, 20))


class TestCodexEffortFromThreadSettings(unittest.TestCase):
    """Codex announces effort in two places. `turn_context.effort` is the common
    one, but `thread_settings_applied.reasoning_effort` sometimes changes it
    mid-session without turn_context repeating the new value (6 of 131 real
    announcements disagreed with the standing turn_context effort), so both feed
    the same last-writer-wins state."""

    def test_thread_settings_changes_effort_without_a_turn_context(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "rollout-x.jsonl")
        recs = [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s", "cwd": "/p/q"}},
            {"timestamp": "t1", "type": "turn_context",
             "payload": {"model": "gpt-5.5", "effort": "low"}},
            {"timestamp": "t2", "type": "event_msg", "payload": {"type": "token_count",
             "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 0,
                                           "output_tokens": 10}}}},
            {"timestamp": "t3", "type": "event_msg",
             "payload": {"type": "thread_settings_applied",
                         "thread_settings": {"reasoning_effort": "xhigh"}}},
            {"timestamp": "t4", "type": "event_msg", "payload": {"type": "token_count",
             "info": {"last_token_usage": {"input_tokens": 200, "cached_input_tokens": 0,
                                           "output_tokens": 20}}}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        _, turns, _ = parse_file(path)
        self.assertEqual([t["effort"] for t in turns], ["low", "xhigh"])


class TestCodexDuplicateEmission(unittest.TestCase):
    """Codex re-emits the same cumulative total on duplicate token_count events
    (different timestamps, no new usage). Summing last_token_usage would double-count;
    the parser must diff the cumulative total and skip zero-delta re-emissions."""

    def _parse(self, recs):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "rollout-x.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return parse_file(path)[1]

    def _tc(self, ts, inp, cached, out, reason=0):
        return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": inp - 0, "cached_input_tokens": cached,
                                              "output_tokens": out, "reasoning_output_tokens": reason},
                         "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                               "output_tokens": out, "reasoning_output_tokens": reason}}}}

    def test_repeated_total_is_skipped(self):
        recs = [
            {"timestamp": "t0", "type": "turn_context", "payload": {"model": "gpt-5.5"}},
            self._tc("t1", 1000, 100, 50),   # cumulative -> delta 1000/100/50
            self._tc("t2", 1000, 100, 50),   # SAME total, later ts -> duplicate, skip
            self._tc("t3", 1500, 200, 80),   # cumulative -> delta 500/100/30
        ]
        turns = self._parse(recs)
        self.assertEqual(len(turns), 2)
        self.assertEqual((turns[0]["input_tokens"], turns[0]["cache_read_tokens"]), (900, 100))
        self.assertEqual((turns[1]["input_tokens"], turns[1]["cache_read_tokens"]), (400, 100))
        # raw totals reconcile to the authoritative final cumulative (1500/200/80)
        self.assertEqual(sum(t["input_tokens"] + t["cache_read_tokens"] for t in turns), 1500)
        self.assertEqual(sum(t["output_tokens"] for t in turns), 80)

    def test_backwards_total_treated_as_reset(self):
        recs = [
            {"timestamp": "t0", "type": "turn_context", "payload": {"model": "gpt-5.5"}},
            self._tc("t1", 1000, 100, 50),   # delta 1000/100/50
            self._tc("t2", 300, 30, 20),     # total dropped -> reset: delta = 300/30/20
        ]
        turns = self._parse(recs)
        self.assertEqual(len(turns), 2)
        self.assertEqual((turns[1]["input_tokens"], turns[1]["cache_read_tokens"],
                          turns[1]["output_tokens"]), (270, 30, 20))


class TestCodexEngine(unittest.TestCase):
    """End-to-end through sources.scan_all: ingestion, reconcile, dedup, idempotency."""

    def _scan(self, codex_home):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        stats = sources.scan_all(db_path=db, verbose=False,
                                 sources=[CodexSource(codex_path=codex_home)])
        return db, stats

    def test_ingest_and_reconcile(self):
        home = _make_codex_home({"rollout-a.jsonl": GOLDEN_CODEX})
        db, stats = self._scan(home)
        self.assertEqual(stats["codex"]["turns"], 3)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT COUNT(*) c, SUM(input_tokens) i, SUM(cache_read_tokens) cr, "
                           "SUM(output_tokens) o, SUM(reasoning_tokens) r FROM turns").fetchone()
        self.assertEqual((row["c"], row["i"], row["cr"], row["o"], row["r"]),
                         (3, 1800, 1700, 330, 130))
        # session rollup reconciled from turns, agent tagged
        s = conn.execute("SELECT total_input_tokens, total_cache_read, turn_count, "
                         "model, agent FROM sessions WHERE session_id='codex-golden-1'").fetchone()
        self.assertEqual((s["total_input_tokens"], s["total_cache_read"], s["turn_count"]),
                         (1800, 1700, 3))
        self.assertEqual(s["agent"], "codex")
        self.assertEqual(s["model"], "gpt-5.5")  # most-common per-turn model
        conn.close()

    def test_idempotent_rescan(self):
        home = _make_codex_home({"rollout-a.jsonl": GOLDEN_CODEX})
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        src = [CodexSource(codex_path=home)]
        sources.scan_all(db_path=db, verbose=False, sources=src)
        st2 = sources.scan_all(db_path=db, verbose=False, sources=src)
        self.assertEqual(st2["codex"]["new"], 0)
        self.assertEqual(st2["codex"]["turns"], 0)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 3)
        conn.close()

    def test_stats_have_no_private_keys(self):
        home = _make_codex_home({"rollout-a.jsonl": GOLDEN_CODEX})
        _, stats = self._scan(home)
        self.assertNotIn("_changed", stats["codex"])

    def test_source_aware_processed_files_no_data_loss(self):
        # If two sources discover the SAME path, the source-aware processed_files
        # lookup must let each ingest it. With the old path-only key, Claude
        # recording the path first made Codex skip it and ingest nothing.
        from sources.claude import ClaudeSource
        home = _make_codex_home({"rollout-a.jsonl": GOLDEN_CODEX})
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        # ClaudeSource pointed at the codex tree finds the same .jsonl (0 claude
        # turns) and records it processed; CodexSource must STILL ingest it.
        sources.scan_all(db_path=db, verbose=False, sources=[
            ClaudeSource(projects_dirs=[home]),
            CodexSource(codex_path=home),
        ])
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM turns WHERE agent='codex'").fetchone()[0], 3)
        conn.close()

    def test_meta_only_then_usage_fixes_agent_and_model(self):
        # An in-progress session scanned with only session_meta/turn_context (no
        # usage yet) must still be agent='codex'; once usage is appended and
        # rescanned, agent stays 'codex' and the NULL model adopts the real model.
        import time
        home = tempfile.mkdtemp()
        sess = Path(home) / "sessions" / "2026" / "01" / "01"
        sess.mkdir(parents=True)
        fp = sess / "rollout-x.jsonl"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
                                "payload": {"id": "cs1", "cwd": "/p/q", "cli_version": "0.130.0"}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-01-01T00:00:01Z", "type": "turn_context",
                                "payload": {"model": "gpt-5.5"}}) + "\n")
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        src = [CodexSource(codex_path=home)]
        sources.scan_all(db_path=db, verbose=False, sources=src)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT agent, turn_count FROM sessions WHERE session_id='cs1'").fetchone()
        self.assertEqual(row["agent"], "codex")   # not the 'claude' default
        self.assertEqual(row["turn_count"], 0)
        conn.close()

        # Append usage, force a rescan (bump mtime), and re-scan.
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
                                "payload": {"type": "token_count", "info": {"total_token_usage": {
                                    "input_tokens": 1000, "cached_input_tokens": 100,
                                    "output_tokens": 50, "reasoning_output_tokens": 5}}}}) + "\n")
        future = time.time() + 10
        os.utime(fp, (future, future))
        sources.scan_all(db_path=db, verbose=False, sources=src)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT agent, model, turn_count, total_input_tokens, "
                           "total_cache_read FROM sessions WHERE session_id='cs1'").fetchone()
        self.assertEqual(row["agent"], "codex")
        self.assertEqual(row["model"], "gpt-5.5")
        self.assertEqual(row["turn_count"], 1)
        self.assertEqual((row["total_input_tokens"], row["total_cache_read"]), (900, 100))
        conn.close()

    def test_branch_copy_dedup(self):
        # Two session files with identical usage content (a branched/resumed session
        # copies the parent history) must NOT double-count: synthetic ids collapse.
        home = _make_codex_home({
            "rollout-parent.jsonl": GOLDEN_CODEX,
            "rollout-child.jsonl": GOLDEN_CODEX,
        })
        db, stats = self._scan(home)
        conn = sqlite3.connect(db)
        # 2 files parsed (6 turns total) but only 3 distinct turns inserted.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 3)
        conn.close()


class TestCodexSchemaCanary(unittest.TestCase):
    def test_every_golden_codex_record_conforms(self):
        for i, record in enumerate(_load_records(GOLDEN_CODEX), 1):
            with self.subTest(record=i):
                self.assertTrue(schema.codex_record_is_known(record),
                                f"record {i} drift: {schema.inspect_codex_record(record)}")

    def test_critical_usage_keys_allowlisted(self):
        for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens", "total_tokens"):
            self.assertIn(key, schema.KNOWN_CODEX_USAGE_KEYS)

    def test_token_count_payload_type_allowlisted(self):
        self.assertIn("token_count", schema.KNOWN_CODEX_PAYLOAD_TYPES)


class TestCodexInspectDrift(unittest.TestCase):
    def test_new_usage_key_flagged(self):
        rec = {"timestamp": "x", "type": "event_msg", "payload": {"type": "token_count",
               "info": {"last_token_usage": {"input_tokens": 1, "quantum_tokens": 9}}}}
        info = schema.inspect_codex_record(rec)
        self.assertIn("quantum_tokens", info["unknown_usage_keys"])
        self.assertFalse(schema.codex_record_is_known(rec))

    def test_new_payload_type_flagged(self):
        rec = {"timestamp": "x", "type": "event_msg", "payload": {"type": "telepathy_call"}}
        self.assertEqual(schema.inspect_codex_record(rec)["unknown_payload_type"], "telepathy_call")

    def test_new_envelope_type_flagged(self):
        rec = {"timestamp": "x", "type": "warp_record", "payload": {}}
        self.assertEqual(schema.inspect_codex_record(rec)["unknown_type"], "warp_record")

    def test_new_envelope_key_flagged(self):
        rec = {"timestamp": "x", "type": "session_meta", "payload": {}, "extraKey": 1}
        self.assertIn("extraKey", schema.inspect_codex_record(rec)["unknown_envelope_keys"])

    def test_new_info_key_flagged(self):
        rec = {"timestamp": "x", "type": "event_msg", "payload": {"type": "token_count",
               "info": {"holographic_window": 1}}}
        self.assertIn("holographic_window", schema.inspect_codex_record(rec)["unknown_info_keys"])

    def test_null_info_is_not_drift(self):
        rec = {"timestamp": "x", "type": "event_msg",
               "payload": {"type": "token_count", "info": None}}
        self.assertTrue(schema.codex_record_is_known(rec))

    def test_wrong_info_type_is_shape_drift(self):
        rec = {"timestamp": "x", "type": "event_msg",
               "payload": {"type": "token_count", "info": "oops"}}
        info = schema.inspect_codex_record(rec)
        self.assertTrue(any(d.startswith("info:") for d in info["shape_drift"]))

    def test_cli_version_echoed_from_session_meta(self):
        rec = {"timestamp": "x", "type": "session_meta", "payload": {"cli_version": "9.9.9"}}
        self.assertEqual(schema.inspect_codex_record(rec)["version"], "9.9.9")


class TestCodexDoctor(unittest.TestCase):
    def test_clean_over_fixture_tree(self):
        home = _make_codex_home({"rollout-a.jsonl": GOLDEN_CODEX})
        report = schema.codex_doctor_scan([home])
        self.assertTrue(schema.codex_report_is_clean(report),
                        schema.format_codex_doctor_report(report))
        self.assertEqual(report["records_scanned"], 13)
        self.assertIn("0.130.0", report["versions"])

    def test_detects_injected_drift(self):
        home = tempfile.mkdtemp()
        sess = Path(home) / "sessions"
        sess.mkdir()
        with open(sess / "rollout-drift.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "x", "type": "warp_record",
                                "payload": {"type": "token_count",
                                            "info": {"last_token_usage": {"warp_tokens": 1}}},
                                "newKey": 1}) + "\n")
        report = schema.codex_doctor_scan([home])
        self.assertFalse(schema.codex_report_is_clean(report))
        self.assertIn("warp_record", report["unknown_types"])
        self.assertIn("warp_tokens", report["unknown_usage_keys"])
        self.assertIn("newKey", report["unknown_envelope_keys"])

    def test_missing_dir_is_empty_not_error(self):
        report = schema.codex_doctor_scan([Path("/nonexistent/codex/home")])
        self.assertEqual(report["records_scanned"], 0)
        self.assertTrue(schema.codex_report_is_clean(report))

    def test_versions_do_not_gate(self):
        # An unseen CLI version alone must NOT fail the report (alpha churn).
        home = tempfile.mkdtemp()
        sess = Path(home) / "sessions"
        sess.mkdir()
        with open(sess / "rollout-v.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "x", "type": "session_meta",
                                "payload": {"id": "s", "cli_version": "99.0.0-alpha.1"}}) + "\n")
        report = schema.codex_doctor_scan([home])
        self.assertIn("99.0.0-alpha.1", report["versions"])
        self.assertTrue(schema.codex_report_is_clean(report))


def _write_rollout(home, name, recs):
    sess = Path(home) / "sessions" / "2026" / "01" / "01"
    sess.mkdir(parents=True, exist_ok=True)
    path = sess / name
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return str(path)


def _tc_total(ts, inp, cached, out):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                           "output_tokens": out}}}}


# A rollout with the extra detail Codex writes beyond token counts: git info on
# session_meta, and tool calls emitted BEFORE the token_count of the response
# that made them (verified ordering against real CLI 0.159 rollouts).
DETAIL_RECS = [
    {"timestamp": "t0", "type": "session_meta",
     "payload": {"id": "codex-detail-1", "cwd": r"C:\work\proj",
                 "git": {"branch": "feature/x", "commit_hash": "abc"}}},
    {"timestamp": "t0", "type": "turn_context", "payload": {"model": "gpt-5.5"}},
    {"timestamp": "t1", "type": "response_item",
     "payload": {"type": "custom_tool_call", "name": "exec", "input": "git status"}},
    {"timestamp": "t1", "type": "response_item",
     "payload": {"type": "custom_tool_call_output", "output": "clean"}},
    _tc_total("t2", 1000, 100, 50),
    _tc_total("t3", 1000, 100, 50),   # duplicate re-emission: must not eat the calls below
    {"timestamp": "t4", "type": "response_item",
     "payload": {"type": "function_call", "name": "sleep", "namespace": "clock",
                 "arguments": "{\"duration_ms\": 45000}"}},
    {"timestamp": "t4", "type": "response_item",
     "payload": {"type": "function_call", "name": "odd", "arguments": "not json"}},
    _tc_total("t5", 1500, 200, 80),
    _tc_total("t6", 1600, 250, 90),   # a response with no tool calls
]


class TestCodexDetail(unittest.TestCase):
    """Tool calls, branch and thread titles — the detail beyond token counts."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = _write_rollout(self.home, "rollout-d.jsonl", DETAIL_RECS)

    def _scan(self, db):
        return sources.scan_all(db_path=db, verbose=False,
                                sources=[CodexSource(codex_path=self.home)])

    def _index(self, *entries):
        with open(Path(self.home) / "session_index.jsonl", "a", encoding="utf-8") as f:
            for sid, name in entries:
                f.write(json.dumps({"id": sid, "thread_name": name}) + "\n")

    def test_tool_calls_attach_to_the_following_turn(self):
        metas, turns, _ = parse_file(self.path)
        self.assertEqual(len(turns), 3)
        first = json.loads(turns[0]["tool_calls"])
        self.assertEqual(first, [{"name": "exec", "input": {"input_size": 25, "command_len": 10}}])
        self.assertEqual(turns[0]["tool_name"], "exec")
        second = json.loads(turns[1]["tool_calls"])
        self.assertEqual([c["name"] for c in second], ["clock.sleep", "odd"])
        self.assertEqual(second[0]["input"], {"input_size": 22})   # duration_ms is not allowlisted
        self.assertEqual(second[1]["input"], {"input_size": 25})   # unparsed arguments dropped
        self.assertEqual(turns[1]["tool_name"], "clock.sleep")
        self.assertIsNone(turns[2]["tool_calls"])
        self.assertIsNone(turns[2]["tool_name"])

    def test_branch_from_session_meta(self):
        metas, _, _ = parse_file(self.path)
        self.assertEqual(metas[0]["git_branch"], "feature/x")

    def test_title_from_session_index_and_rename(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._index(("codex-detail-1", "First name"), ("someone-else", "Nope"))
        self._scan(db)
        conn = sqlite3.connect(db)
        topic = lambda: conn.execute("SELECT topic FROM sessions "
                                     "WHERE session_id='codex-detail-1'").fetchone()[0]
        self.assertEqual(topic(), "First name")
        # A rename appends a later entry; the rollout itself is untouched, so the
        # title must come from the index on every scan, latest entry winning.
        self._index(("codex-detail-1", "Renamed"))
        self._scan(db)
        self.assertEqual(topic(), "Renamed")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        conn.close()

    def test_backfill_fills_rows_scanned_before_detail_support(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        # Simulate a DB from before this change: detail columns empty, marker unset.
        conn.execute("UPDATE turns SET tool_calls = NULL, tool_name = NULL")
        conn.execute("UPDATE sessions SET git_branch = ''")
        conn.execute("DELETE FROM schema_meta WHERE key = 'detail_backfill_files_codex'")
        conn.commit()
        self._scan(db)   # file unchanged -> walk skips it; only the backfill can fill
        rows = conn.execute("SELECT tool_name, tool_calls FROM turns "
                            "ORDER BY timestamp").fetchall()
        self.assertEqual([r[0] for r in rows], ["exec", "clock.sleep", None])
        self.assertEqual(json.loads(rows[0][1])[0]["input"], {"input_size": 25, "command_len": 10})
        self.assertEqual(conn.execute("SELECT git_branch FROM sessions").fetchone()[0],
                         "feature/x")
        conn.close()

    def test_backfill_read_failure_leaves_marker_unset(self):
        from unittest import mock
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE turns SET tool_calls = NULL, tool_name = NULL")
        conn.execute("DELETE FROM schema_meta WHERE key = 'detail_backfill_files_codex'")
        conn.commit()
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            CodexSource(codex_path=self.home).backfill_details(conn)
        self.assertIsNone(conn.execute("SELECT value FROM schema_meta "
                                       "WHERE key = 'detail_backfill_files_codex'").fetchone())
        CodexSource(codex_path=self.home).backfill_details(conn)   # access recovered
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns "
                                      "WHERE tool_calls IS NOT NULL").fetchone()[0], 2)
        conn.close()


    def test_replayed_call_ids_dedup_but_identical_distinct_calls_survive(self):
        recs = json.loads(json.dumps(DETAIL_RECS))
        recs[2]["payload"]["call_id"] = "call-1"
        repeat = json.loads(json.dumps(recs[2]))
        distinct = json.loads(json.dumps(recs[2]))
        distinct["payload"]["call_id"] = "call-2"
        recs[3:3] = [repeat, distinct]
        _write_rollout(self.home, "rollout-d.jsonl", recs)
        _, turns, _ = parse_file(self.path)
        self.assertEqual(len(json.loads(turns[0]["tool_calls"])), 2)
        self.assertEqual([t["message_id"] for t in turns],
                         [t["message_id"] for t in parse_file(
                             _write_rollout(self.home, "rollout-baseline.jsonl", DETAIL_RECS))[1]])

    def test_strict_parse_rejects_malformed_json(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"unfinished":')
        with self.assertRaises(json.JSONDecodeError):
            parse_file(self.path, strict=True)
        self.assertEqual(len(parse_file(self.path)[1]), 3)  # tolerant usage parser unchanged

    def test_branch_only_gap_is_filled_and_known_metadata_survives(self):
        db = os.path.join(self.home, "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE turns SET tool_name = 'known', tool_calls = '[]'")
        conn.execute("UPDATE sessions SET git_branch = ''")
        conn.commit()
        self._scan(db)
        self.assertEqual(conn.execute("SELECT git_branch FROM sessions").fetchone()[0], "feature/x")
        self.assertEqual(conn.execute("SELECT DISTINCT tool_name, tool_calls FROM turns").fetchall(),
                         [('known', '[]')])
        conn.close()

    def test_pruned_then_restored_file_retries_despite_old_global_marker(self):
        db = os.path.join(self.home, "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        snapshot = conn.execute("SELECT message_id, input_tokens, output_tokens, cache_read_tokens "
                                "FROM turns ORDER BY message_id").fetchall()
        conn.execute("UPDATE turns SET tool_calls = '', tool_name = ''")
        conn.execute("UPDATE sessions SET git_branch = ''")
        conn.execute("DELETE FROM schema_meta WHERE key = 'detail_backfill_files_codex'")
        conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('detail_backfill_done_codex', '1')")
        conn.commit()
        hidden = self.path + '.hidden'
        os.rename(self.path, hidden)
        self._scan(db)
        self.assertEqual(conn.execute("SELECT tool_calls FROM turns LIMIT 1").fetchone()[0], '')
        os.rename(hidden, self.path)
        self._scan(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE tool_calls != ''").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT git_branch FROM sessions").fetchone()[0], "feature/x")
        self.assertEqual(conn.execute("SELECT message_id, input_tokens, output_tokens, cache_read_tokens "
                                     "FROM turns ORDER BY message_id").fetchall(), snapshot)
        conn.close()

    def test_incomplete_file_does_not_receipt_and_repairs_on_retry(self):
        db = os.path.join(self.home, "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE turns SET tool_calls = NULL, tool_name = NULL")
        conn.commit()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"unfinished":')
        self._scan(db)
        self.assertIsNone(conn.execute("SELECT value FROM schema_meta WHERE "
                                       "key = 'detail_backfill_files_codex'").fetchone())
        _write_rollout(self.home, "rollout-d.jsonl", DETAIL_RECS)
        self._scan(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE tool_calls IS NOT NULL").fetchone()[0], 2)
        conn.close()

    def test_unchanged_complete_file_is_not_reparsed(self):
        from unittest import mock
        db = os.path.join(self.home, "t.db")
        # Settled files: older than the racy-mtime window, so their stamp is trusted.
        old = time.time() - 60
        for p in Path(self.home, "sessions").rglob("*.jsonl"):
            os.utime(p, (old, old))
        self._scan(db)
        self._scan(db)  # receipts existing rows
        with mock.patch('sources.codex.parse_file', side_effect=AssertionError('unexpected parse')):
            self._scan(db)

    def test_detail_branch_copy_dedup_preserves_all_token_totals(self):
        recs = json.loads(json.dumps(DETAIL_RECS))
        recs[0]["payload"]["id"] = 'branch-copy'
        _write_rollout(self.home, "rollout-copy.jsonl", recs)
        db = os.path.join(self.home, "t.db")
        self._scan(db)
        self._scan(db)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*), SUM(input_tokens + cache_read_tokens), "
                                     "SUM(output_tokens) FROM turns").fetchone(), (3, 1600, 90))
        self.assertEqual(conn.execute("SELECT SUM(turn_count), SUM(total_input_tokens + total_cache_read), "
                                     "SUM(total_output_tokens) FROM sessions").fetchone(), (3, 1600, 90))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE tool_calls IS NOT NULL").fetchone()[0], 2)
        conn.close()

    def test_user_title_is_preserved(self):
        db = os.path.join(self.home, 't.db')
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE sessions SET topic = 'User title', topic_source = 'custom'")
        conn.commit()
        self._index(('codex-detail-1', 'Automatic title'))
        self._scan(db)
        self.assertEqual(conn.execute('SELECT topic, topic_source FROM sessions').fetchone(),
                         ('User title', 'custom'))
        conn.close()



    def test_file_growing_during_read_is_retried_without_partial_updates(self):
        from unittest import mock
        db = os.path.join(self.home, "t.db")
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE turns SET tool_calls = NULL, tool_name = NULL")
        conn.commit()
        def growing_read(filepath, **kwargs):
            parsed = parse_file(filepath, **kwargs)
            stat = os.stat(filepath)
            os.utime(filepath, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
            return parsed
        with mock.patch('sources.codex.parse_file', side_effect=growing_read):
            CodexSource(codex_path=self.home).backfill_details(conn)
        self.assertIsNone(conn.execute("SELECT value FROM schema_meta WHERE "
                                       "key = 'detail_backfill_files_codex'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE tool_calls IS NOT NULL").fetchone()[0], 0)
        CodexSource(codex_path=self.home).backfill_details(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE tool_calls IS NOT NULL").fetchone()[0], 2)
        conn.close()

    def test_failed_file_does_not_block_other_files_or_get_receipted(self):
        from unittest import mock
        db = os.path.join(self.home, 't.db')
        bad = _write_rollout(self.home, 'rollout-failed.jsonl', DETAIL_RECS)
        self._scan(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('UPDATE turns SET tool_calls = NULL, tool_name = NULL')
        conn.commit()
        def selective_read(filepath, **kwargs):
            if filepath == bad:
                raise PermissionError('locked')
            return parse_file(filepath, **kwargs)
        with mock.patch('sources.codex.parse_file', side_effect=selective_read):
            CodexSource(codex_path=self.home).backfill_details(conn)
        receipts = json.loads(conn.execute("SELECT value FROM schema_meta WHERE "
                                           "key = 'detail_backfill_files_codex'").fetchone()[0])
        self.assertIn(self.path, receipts)
        self.assertNotIn(bad, receipts)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM turns WHERE tool_calls IS NOT NULL').fetchone()[0], 2)
        CodexSource(codex_path=self.home).backfill_details(conn)
        receipts = json.loads(conn.execute("SELECT value FROM schema_meta WHERE "
                                           "key = 'detail_backfill_files_codex'").fetchone()[0])
        self.assertIn(bad, receipts)
        conn.close()



    def test_zero_delta_emission_does_not_consume_pending_calls(self):
        recs = json.loads(json.dumps(DETAIL_RECS))
        recs.insert(5, {"timestamp": "t3", "type": "response_item", "payload": {
            "type": "function_call", "name": "queued", "call_id": "queued-1", "arguments": "{}"}})
        _write_rollout(self.home, "rollout-d.jsonl", recs)
        _, turns, _ = parse_file(self.path)
        self.assertEqual(len(turns), 3)
        self.assertEqual([c["name"] for c in json.loads(turns[1]["tool_calls"])],
                         ['queued', 'clock.sleep', 'odd'])


if __name__ == "__main__":
    unittest.main()
