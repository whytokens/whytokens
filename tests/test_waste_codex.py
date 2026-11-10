"""Codex resident_output: inferred from per-request input growth after a tool call.

Fixtures are synthetic rollouts (tests/fixtures/codex/waste-*.jsonl), scanned through the real
Codex source. waste-resident: request 3 calls a tool, request 4 grows 60k fresh tokens.
waste-clean: same shape, flat growth, no tool.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sources
import waste
from sources.codex import CodexSource

FIX = Path(__file__).resolve().parent / "fixtures" / "codex"


class TestCodexResident(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = tempfile.mkdtemp()
        sess = Path(home) / "sessions" / "2026" / "08" / "20"
        sess.mkdir(parents=True)
        for n in ("waste-resident", "waste-clean"):
            shutil.copy(FIX / f"{n}.jsonl", sess / f"rollout-{n}.jsonl")
        cls.db = os.path.join(tempfile.mkdtemp(), "t.db")
        sources.scan_all(db_path=cls.db, verbose=False, sources=[CodexSource(codex_path=home)])

    def test_clean_session_reports_nothing(self):
        self.assertEqual(waste.report(self.db, session_id="codex-waste-clean"), [])

    def test_growth_after_tool_is_flagged_and_tagged_inferred(self):
        f = waste.report(self.db, session_id="codex-waste-resident")
        self.assertEqual([x["cause"] for x in f], ["resident_output"])
        self.assertEqual(f[0]["tier"], "inferred")
        self.assertTrue(f[0]["advice"].startswith("Inferred"))
        self.assertIn("60,000", f[0]["detail"])
        self.assertIn("shell", f[0]["detail"])
        self.assertNotIn("Explore", f[0]["fix"])  # Claude Code tools don't exist in Codex
        self.assertIn("tail", f[0]["fix"])

    def test_growth_without_a_tool_is_not_charged(self):
        import sqlite3
        copy = os.path.join(tempfile.mkdtemp(), "c.db")
        shutil.copy(self.db, copy)
        c = sqlite3.connect(copy)
        c.execute("UPDATE turns SET tool_name=NULL, tool_calls=NULL")
        c.commit(); c.close()
        self.assertEqual(waste.report(copy, session_id="codex-waste-resident"), [])

    def test_mixed_source_session_keeps_each_detector(self):
        # a resumed session id can hold both sources: make the cold first turn claude: its uncached_input stays a claude
        # observation and the codex finding survives
        import sqlite3
        copy = os.path.join(tempfile.mkdtemp(), "m.db")
        shutil.copy(self.db, copy)
        c = sqlite3.connect(copy)
        c.execute("UPDATE turns SET agent='claude' WHERE session_id='codex-waste-resident' AND timestamp < '2026-08-20T10:02'")
        c.commit(); c.close()
        f = waste.report(copy, session_id="codex-waste-resident")
        self.assertEqual([(x["cause"], x["tier"]) for x in f], [("resident_output", "inferred"), ("uncached_input", "observation")])

    def test_measurement_cell_is_inferred_not_unmeasurable(self):
        cell = waste.measurement(self.db)["causes"]["resident_output"]["codex"]
        self.assertEqual(cell["status"], "inferred")
        self.assertIn("inferred", cell["reason"])
        self.assertEqual(waste.measurement(self.db)["causes"]["idle_gap"]["codex"]["status"], "not measurable")


if __name__ == "__main__":
    unittest.main()
