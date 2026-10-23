"""Tests for schema.py (drift armor) + the golden corpus.

Three jobs:
  * Golden extraction — the committed fixtures parse to the exact tokens / tools /
    flags we expect, so a parser regression shows up here.
  * Schema-surface canary — every committed fixture record conforms to the
    allowlists, and the allowlists still hold the critical keys. Refreshing the
    fixtures from a newer Claude Code release with a brand-new key turns this red.
  * Doctor — doctor_scan() is clean over the fixtures and flags injected drift.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import schema
from scanner import parse_jsonl_file

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Claude and Codex golden corpora live in disjoint subtrees so each source's
# doctor scans only its own envelope (mirroring real ~/.claude vs ~/.codex).
CLAUDE_FIXTURES = FIXTURES / "claude"
GOLDEN = CLAUDE_FIXTURES / "golden.jsonl"


def _load_records(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class TestGoldenExtraction(unittest.TestCase):
    """parse_jsonl_file over the golden corpus extracts the expected turns."""

    @classmethod
    def setUpClass(cls):
        metas, turns, line_count = parse_jsonl_file(str(GOLDEN))
        cls.metas = metas
        cls.turns = turns
        cls.line_count = line_count
        cls.by_id = {t["message_id"]: t for t in turns}

    def test_line_count(self):
        self.assertEqual(self.line_count, 22)

    def test_two_sessions_seen(self):
        self.assertEqual(
            sorted(m["session_id"] for m in self.metas),
            ["golden-sess-1", "golden-sess-2"],
        )

    def test_only_token_bearing_turns_recorded(self):
        # 10 turns: 6 main + 1 subagent (+ its synthetic agent_summary) + the advisor-iteration
        # record, which yields TWO turns (its own, plus the advisor_message iteration
        # emitted as a separate turn on the advisorModel). The zero-usage error
        # envelopes, the user records (incl. the subagent-dispatch tool_result), the
        # ai-title / custom-title records, and the workflow-journal / frame-link
        # records do not become turns.
        self.assertEqual(len(self.turns), 10)

    def test_subagent_turn_carries_agent_id(self):
        t = self.by_id["msg_subagent_001"]
        self.assertEqual(t["agent_id"], "agent-explore-1")
        self.assertEqual(t["is_sidechain"], 1)  # agentId → subagent detection

    def test_custom_title_wins_over_ai_title(self):
        # golden-sess-1 carries both an ai-title and a later custom-title record;
        # the user-set custom-title must win regardless of record order.
        meta = next(m for m in self.metas if m["session_id"] == "golden-sess-1")
        self.assertEqual(meta["topic"], "Widget loader cleanup")

    def test_basic_turn_tokens_and_model(self):
        t = self.by_id["msg_basic_001"]
        self.assertEqual(t["model"], "claude-opus-4-8")
        self.assertEqual(t["input_tokens"], 1200)
        self.assertEqual(t["output_tokens"], 350)
        self.assertEqual(t["cache_read_tokens"], 8000)
        self.assertEqual(t["cache_creation_tokens"], 500)
        self.assertEqual(t["service_tier"], "standard")

    def test_cache_creation_breakdown(self):
        t = self.by_id["msg_cache_001"]
        self.assertEqual(t["cache_creation_5m_tokens"], 400)
        self.assertEqual(t["cache_creation_1h_tokens"], 100)

    def test_tool_calls_captured(self):
        t = self.by_id["msg_tool_001"]
        self.assertEqual(t["tool_name"], "Read")  # first tool_use block
        calls = json.loads(t["tool_calls"])
        self.assertEqual([c["name"] for c in calls], ["Read", "Grep"])

    def test_sidechain_flagged(self):
        self.assertEqual(self.by_id["msg_side_001"]["is_sidechain"], 1)

    def test_advisor_era_turn_ingested(self):
        # Record 12 carries the 2.1.170+ envelope (advisorModel, session_id,
        # iterations/speed usage keys); the turn must still parse normally.
        t = self.by_id["msg_advisor_001"]
        self.assertEqual(t["input_tokens"], 500)
        self.assertEqual(t["model"], "claude-opus-4-8")

    def test_container_context_turn_ingested(self):
        # Record 6 carries the recent container/context_management message keys;
        # the turn itself must still parse normally.
        t = self.by_id["msg_ctx_001"]
        self.assertEqual(t["input_tokens"], 111)


class TestSchemaCanary(unittest.TestCase):
    """The golden corpus stays inside the known schema surface."""

    def test_every_golden_record_conforms(self):
        for i, record in enumerate(_load_records(GOLDEN), 1):
            with self.subTest(record=i):
                info = schema.inspect_record(record)
                self.assertEqual(info["unknown_record_keys"], [],
                                 f"record {i} top-level drift: {info}")
                self.assertEqual(info["unknown_message_keys"], [],
                                 f"record {i} message drift: {info}")
                self.assertEqual(info["unknown_usage_keys"], [],
                                 f"record {i} usage drift: {info}")
                self.assertEqual(info["unknown_cache_creation_keys"], [],
                                 f"record {i} cache_creation drift: {info}")
                self.assertIsNone(info["unknown_type"],
                                  f"record {i} unknown type: {info}")

    def test_critical_usage_keys_allowlisted(self):
        # Guard against an allowlist edit that would silently stop flagging the
        # money-bearing token buckets.
        for key in ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens"):
            self.assertIn(key, schema.KNOWN_USAGE_KEYS)

    def test_ingested_types_allowlisted(self):
        self.assertIn("assistant", schema.KNOWN_RECORD_TYPES)
        self.assertIn("user", schema.KNOWN_RECORD_TYPES)
        self.assertIn("custom-title", schema.KNOWN_RECORD_TYPES)
        self.assertIn("ai-title", schema.KNOWN_RECORD_TYPES)
        self.assertIn("customTitle", schema.KNOWN_RECORD_KEYS)
        self.assertIn("aiTitle", schema.KNOWN_RECORD_KEYS)

    def test_cache_creation_breakdown_keys_allowlisted(self):
        self.assertIn("ephemeral_5m_input_tokens", schema.KNOWN_CACHE_CREATION_KEYS)
        self.assertIn("ephemeral_1h_input_tokens", schema.KNOWN_CACHE_CREATION_KEYS)

    def test_2_1_170_surface_allowlisted(self):
        # The 2.1.170+ additions: workflow-journal + frame-link record types and
        # the new metadata keys. None carry tokens; recognised-and-ignored.
        for rtype in ("started", "result", "frame-link"):
            self.assertIn(rtype, schema.KNOWN_RECORD_TYPES)
        for key in ("advisorModel", "session_id", "key", "result", "toolEndsTurn",
                    "toolDenialKind", "classifierMetaLines", "apiError",
                    "errorDetails", "path", "frameUrl"):
            self.assertIn(key, schema.KNOWN_RECORD_KEYS)

    def test_subagent_envelope_keys_allowlisted(self):
        # The subagent-dispatch port reads these top-level keys; they must stay in
        # the allowlist so a subagent record doesn't read as drift. (The dispatch
        # sub-fields live inside toolUseResult, which schema.py treats opaquely.)
        for key in ("agentId", "data", "toolUseResult"):
            self.assertIn(key, schema.KNOWN_RECORD_KEYS)


class TestInspectRecordDrift(unittest.TestCase):
    """inspect_record actually catches new keys / types (not just passes)."""

    def test_new_usage_key_flagged(self):
        record = {
            "type": "assistant", "sessionId": "s",
            "message": {"usage": {"input_tokens": 1, "quantum_tokens": 9}},
        }
        info = schema.inspect_record(record)
        self.assertIn("quantum_tokens", info["unknown_usage_keys"])
        self.assertFalse(schema.record_is_known(record))

    def test_new_top_level_key_flagged(self):
        record = {"type": "user", "sessionId": "s", "telepathyField": True}
        info = schema.inspect_record(record)
        self.assertIn("telepathyField", info["unknown_record_keys"])

    def test_new_message_key_flagged(self):
        record = {"type": "assistant", "message": {"hologram": 1}}
        self.assertIn("hologram", schema.inspect_record(record)["unknown_message_keys"])

    def test_unknown_type_flagged(self):
        record = {"type": "telekinesis", "sessionId": "s"}
        self.assertEqual(schema.inspect_record(record)["unknown_type"], "telekinesis")

    def test_new_cache_creation_key_flagged(self):
        record = {"type": "assistant", "message": {"usage": {
            "cache_creation": {"ephemeral_3d_input_tokens": 5}}}}
        info = schema.inspect_record(record)
        self.assertIn("ephemeral_3d_input_tokens", info["unknown_cache_creation_keys"])

    def test_non_dict_record_handled(self):
        info = schema.inspect_record(["not", "a", "dict"])
        self.assertEqual(info["unknown_type"], "<not-an-object>")
        self.assertEqual(info["shape_drift"], [])

    def test_non_dict_message_flagged_as_shape_drift(self):
        # message must be a dict; the scanner assumes msg.get(...). A list here is
        # real drift, not "clean" — guards against a silent false negative.
        record = {"type": "assistant", "sessionId": "s", "message": []}
        info = schema.inspect_record(record)
        self.assertTrue(any(d.startswith("message:") for d in info["shape_drift"]))
        self.assertFalse(schema.record_is_known(record))

    def test_non_dict_usage_flagged(self):
        record = {"type": "assistant", "message": {"usage": "oops"}}
        info = schema.inspect_record(record)
        self.assertTrue(any(d.startswith("usage:") for d in info["shape_drift"]))

    def test_non_dict_cache_creation_flagged(self):
        record = {"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_creation": 5}}}
        info = schema.inspect_record(record)
        self.assertTrue(any(d.startswith("cache_creation:") for d in info["shape_drift"]))

    def test_version_echoed(self):
        record = {"type": "assistant", "version": "9.9.9"}
        self.assertEqual(schema.inspect_record(record)["version"], "9.9.9")


class TestDoctorScan(unittest.TestCase):
    def test_clean_over_golden_corpus(self):
        report = schema.doctor_scan([CLAUDE_FIXTURES])
        self.assertTrue(schema.report_is_clean(report),
                        schema.format_doctor_report(report))
        self.assertEqual(report["records_scanned"], 22)
        self.assertEqual(report["unseen_versions"], [])

    def test_detects_injected_drift(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "drift.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "warp-record", "sessionId": "s", "version": "99.0.0",
                "newTopKey": 1,
                "message": {"usage": {"input_tokens": 1, "newUsageKey": 2}},
            }) + "\n")
        report = schema.doctor_scan([Path(tmpdir)])
        self.assertFalse(schema.report_is_clean(report))
        self.assertIn("warp-record", report["unknown_types"])
        self.assertIn("newTopKey", report["unknown_record_keys"])
        self.assertIn("newUsageKey", report["unknown_usage_keys"])
        self.assertIn("99.0.0", report["unseen_versions"])

    def test_missing_dir_is_empty_not_error(self):
        report = schema.doctor_scan([Path("/nonexistent/projects/dir")])
        self.assertEqual(report["records_scanned"], 0)
        self.assertTrue(schema.report_is_clean(report))

    def test_file_limit_caps_scan(self):
        report = schema.doctor_scan([CLAUDE_FIXTURES], file_limit=0)
        self.assertEqual(report["files_scanned"], 0)
        self.assertEqual(report["records_scanned"], 0)

    def test_reports_non_object_records(self):
        # A JSON line that parses but isn't an object must be counted + flagged,
        # not silently skipped.
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "weird.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]\n")
            f.write("42\n")
        report = schema.doctor_scan([Path(tmpdir)])
        self.assertEqual(report["records_scanned"], 2)
        self.assertFalse(schema.report_is_clean(report))
        self.assertIn("<not-an-object>", report["unknown_types"])

    def test_reports_shape_drift(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "shape.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": ["wrong"]}) + "\n")
        report = schema.doctor_scan([Path(tmpdir)])
        self.assertFalse(schema.report_is_clean(report))
        self.assertTrue(any(k.startswith("message:") for k in report["shape_drift"]))


class TestCodexZeroOnlyUsageCanary(unittest.TestCase):
    """`cache_write_input_tokens` is allowlisted only because Codex has never
    populated it. Allowlisting a key normally removes its tripwire, so a
    *value*-level canary replaces it: the first nonzero value is drift. These
    tests are the committed falsification of that canary — they must go red if
    the check is removed."""

    @staticmethod
    def _token_count(cache_write):
        return {
            "timestamp": "2026-08-01T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 1000, "cached_input_tokens": 200,
                        "cache_write_input_tokens": cache_write,
                        "output_tokens": 50, "reasoning_output_tokens": 10,
                        "total_tokens": 1050,
                    },
                    "total_token_usage": {
                        "input_tokens": 1000, "cached_input_tokens": 200,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 50, "reasoning_output_tokens": 10,
                        "total_tokens": 1050,
                    },
                    "model_context_window": 258400,
                },
            },
        }

    def test_zero_is_not_drift(self):
        """The status quo (field present, always 0) must stay clean, or the
        canary is just the old false alarm under a new name."""
        info = schema.inspect_codex_record(self._token_count(0))
        self.assertEqual(info["unknown_usage_keys"], [])
        self.assertEqual(info["nonzero_zero_only_usage_keys"], [])

    def test_nonzero_is_drift(self):
        info = schema.inspect_codex_record(self._token_count(512))
        self.assertEqual(info["nonzero_zero_only_usage_keys"],
                         ["cache_write_input_tokens"])
        # Still a *known* key — this is a value alarm, not an unknown-key alarm.
        self.assertEqual(info["unknown_usage_keys"], [])

    def test_stringified_nonzero_is_drift(self):
        """Codex stringifies numbers on some CLI versions (see sources/codex.py
        `_int`); "512" must trip the canary just like 512."""
        info = schema.inspect_codex_record(self._token_count("512"))
        self.assertEqual(info["nonzero_zero_only_usage_keys"],
                         ["cache_write_input_tokens"])

    def _scan(self, cache_write):
        # _iter_codex_jsonl only looks under sessions/ for rollout-*.jsonl, so
        # the layout matters — a stray path scans 0 files and every "clean"
        # assertion below would pass vacuously. Hence the records_scanned check.
        tmpdir = tempfile.mkdtemp()
        sess = Path(tmpdir) / "sessions" / "2026" / "08" / "01"
        sess.mkdir(parents=True)
        with open(sess / "rollout-canary.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(self._token_count(cache_write)) + "\n")
        report = schema.codex_doctor_scan([Path(tmpdir)])
        self.assertEqual(report["records_scanned"], 1, "fixture was not scanned")
        return report

    def test_nonzero_fails_the_doctor_gate(self):
        report = self._scan(512)
        self.assertEqual(report["nonzero_zero_only_usage_keys"],
                         {"cache_write_input_tokens": 1})
        self.assertFalse(schema.codex_report_is_clean(report),
                         schema.format_codex_doctor_report(report))
        self.assertIn("NONZERO", schema.format_codex_doctor_report(report))

    def test_zero_passes_the_doctor_gate(self):
        report = self._scan(0)
        self.assertEqual(report["nonzero_zero_only_usage_keys"], {})
        self.assertTrue(schema.codex_report_is_clean(report),
                        schema.format_codex_doctor_report(report))


if __name__ == "__main__":
    unittest.main()
