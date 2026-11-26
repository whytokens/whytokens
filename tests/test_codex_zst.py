"""Compressed Codex rollouts (rollout-*.jsonl.zst): each decoder path, and the no-decoder warning.

The decoder is mocked so the suite runs on any Python; one test uses the real zstd
executable when it is installed, against the committed binary fixture.
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import scanner
import schema
import sources
from sources import codex
from sources.codex import CodexSource, parse_file

FIX = Path(__file__).resolve().parent / "fixtures" / "codex"
GOLDEN = FIX / "golden-codex.jsonl"
GOLDEN_ZST = FIX / "golden-codex.jsonl.zst"


def _home(*names):
    home = tempfile.mkdtemp()
    d = Path(home) / "sessions" / "2026" / "01" / "01"
    d.mkdir(parents=True)
    for n in names:
        shutil.copy(GOLDEN_ZST if n.endswith(".zst") else GOLDEN, d / n)
    return home, d


def _exe_decoder():
    """Pretend a zstd executable exists and decodes any file to the golden text."""
    def popen(cmd, **kw):
        return types.SimpleNamespace(stdout=io.BytesIO(GOLDEN.read_bytes()), wait=lambda: 0)
    return (mock.patch.object(codex, "zst_decoder", return_value="exe"),
            mock.patch.object(codex, "_zstd_exe", return_value="zstd"),
            mock.patch.object(codex.subprocess, "Popen", side_effect=popen))


def _no_decoder():
    return mock.patch.object(codex, "zst_decoder", return_value=None)


def _scan(home, db=None):
    db = db or os.path.join(tempfile.mkdtemp(), "t.db")
    stats = sources.scan_all(db_path=db, verbose=False, sources=[CodexSource(codex_path=home)])
    return db, stats["codex"]


def _counts(db):
    c = sqlite3.connect(db)
    try:
        return (c.execute("SELECT COUNT(*), SUM(output_tokens) FROM turns").fetchone(),
                c.execute("SELECT turn_count, total_output_tokens FROM sessions").fetchall())
    finally:
        c.close()


class TestZstDecoders(unittest.TestCase):
    def test_exe_path_matches_plain_twin(self):
        _, d = _home("rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            got = parse_file(str(d / "rollout-a.jsonl.zst"))
        want = parse_file(str(GOLDEN))
        self.assertEqual([t["message_id"] for t in got[1]], [t["message_id"] for t in want[1]])
        self.assertEqual(got[2], want[2])

    def test_exe_runs_without_a_console_window(self):
        _, d = _home("rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2] as popen:
            parse_file(str(d / "rollout-a.jsonl.zst"))
        self.assertEqual(popen.call_args.kwargs.get("creationflags"),
                         getattr(codex.subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            self.assertNotEqual(popen.call_args.kwargs["creationflags"], 0)

    def test_exe_nonzero_exit_raises(self):
        _, d = _home("rollout-a.jsonl.zst")
        with mock.patch.object(codex, "zst_decoder", return_value="exe"), \
             mock.patch.object(codex, "_zstd_exe", return_value="zstd"), \
             mock.patch.object(codex.subprocess, "Popen", side_effect=lambda *a, **k:
                               types.SimpleNamespace(stdout=io.BytesIO(b"{}\n"), wait=lambda: 1)):
            with self.assertRaises(OSError):
                parse_file(str(d / "rollout-a.jsonl.zst"))

    def test_unreadable_rollout_does_not_abort_effort_backfill_or_doctor(self):
        home, d = _home("rollout-a.jsonl", "rollout-b.jsonl.zst")
        p = _exe_decoder()
        bad = types.SimpleNamespace(stdout=io.BytesIO(b"{}\n"), wait=lambda: 1)
        with p[0], p[1], mock.patch.object(codex.subprocess, "Popen", return_value=bad):
            conn = sqlite3.connect(":memory:")
            scanner.init_db(conn)
            conn.execute("INSERT INTO turns (session_id, timestamp, model, message_id, agent, effort) "
                         "VALUES ('s', 't', 'm', 'codex:x', 'codex', '')")
            before = codex.subprocess.Popen.call_count
            self.assertEqual(CodexSource(codex_path=home).backfill_effort(conn), 0)
            self.assertGreater(codex.subprocess.Popen.call_count, before)  # reached the decoder
            self.assertIsNone(scanner._meta_get(conn, "effort_backfill_done_codex"))
            rep = schema.codex_doctor_scan([home])
        self.assertEqual(rep["unread_zst"], 1)

    def test_stdlib_module_path(self):
        _, d = _home("rollout-a.jsonl.zst")
        fake = types.ModuleType("compression.zstd")
        fake.open = lambda path, mode, **kw: io.StringIO(GOLDEN.read_text(encoding="utf-8"))
        pkg = types.ModuleType("compression")
        pkg.zstd = fake
        with mock.patch.dict(sys.modules, {"compression": pkg, "compression.zstd": fake}):
            self.assertEqual(codex.zst_decoder(), "module")
            got = parse_file(str(d / "rollout-a.jsonl.zst"))
        self.assertEqual(len(got[1]), 3)

    @unittest.skipUnless(shutil.which("zstd") and codex.zst_decoder() == "exe",
                         "needs the zstd executable and no stdlib module")
    def test_real_executable_reads_committed_fixture(self):
        got = parse_file(str(GOLDEN_ZST))
        self.assertEqual(len(got[1]), 3)


class TestZstEngine(unittest.TestCase):
    def test_discovery_includes_zst_when_decodable(self):
        home, _ = _home("rollout-a.jsonl.zst")
        with _exe_decoder()[0]:
            self.assertEqual(len(codex.discover_files(home)), 1)

    def test_scan_ingests_zst(self):
        home, _ = _home("rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            db, st = _scan(home)
        self.assertEqual(st["turns"], 3)
        self.assertEqual(_counts(db)[0][0], 3)

    def test_file_seen_in_both_forms_does_not_double_count(self):
        home, _ = _home("rollout-a.jsonl", "rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            db, _ = _scan(home)
        want = _counts(_scan(_home("rollout-a.jsonl")[0])[0])
        self.assertEqual(_counts(db), want)

    def test_rename_to_zst_neither_duplicates_nor_drops(self):
        home, d = _home("rollout-a.jsonl")
        db, _ = _scan(home)
        before = _counts(db)
        (d / "rollout-a.jsonl").unlink()
        shutil.copy(GOLDEN_ZST, d / "rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            _, st = _scan(home, db)
        self.assertEqual(st["new"], 1)
        self.assertEqual(_counts(db), before)

    def test_no_decoder_skips_warns_and_never_crashes(self):
        home, _ = _home("rollout-a.jsonl", "rollout-b.jsonl.zst")
        with _no_decoder():
            self.assertEqual(len(codex.discover_files(home)), 1)
            db, st = _scan(home)
            self.assertEqual(st["unreadable"], 1)
            rep = schema.codex_doctor_scan([home])
            text = schema.format_codex_doctor_report(rep)
        self.assertEqual(_counts(db)[0][0], 3)
        self.assertEqual(rep["unread_zst"], 1)
        self.assertIn("NOT READ: 1", text)
        self.assertIn("zstd", text)

    def test_decoder_present_reports_nothing_unread(self):
        home, _ = _home("rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            _, st = _scan(home)
        self.assertNotIn("unreadable", st)

    def test_doctor_reads_zst(self):
        home, _ = _home("rollout-a.jsonl.zst")
        p = _exe_decoder()
        with p[0], p[1], p[2]:
            rep = schema.codex_doctor_scan([home])
        self.assertEqual(rep["files_scanned"], 1)
        self.assertGreater(rep["records_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
