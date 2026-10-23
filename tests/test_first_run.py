"""First ten minutes: empty states, odd paths, unreadable files, port clash,
rescan serialization, and the performance budget. scripts/falsify_first_run.py breaks each rule."""

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import api
import config
import scanner
import scanrun
import sources
from sources.claude import ClaudeSource

from tests.test_server_hardening import Base, TOKEN  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import perf_budget  # noqa: E402


def status_with(tmp, claude_dirs, codex_dir, sessions=0):
    db = tmp / "s.db"
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    for i in range(sessions):
        conn.execute("INSERT INTO sessions (session_id) VALUES (?)", (f"s{i}",))
    conn.commit()
    conn.close()
    with mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", claude_dirs), \
         mock.patch.object(config, "CODEX_PATH", codex_dir):
        return api.get_status(db)


class EmptyStates(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def test_no_transcripts(self):
        st = status_with(self.tmp, [self.tmp / "nope"], self.tmp / "nope2")
        self.assertEqual(st["empty"], "no_transcripts")
        self.assertIn("WHYTOKENS_EXTRA_PROJECTS_DIRS", st["hint"])
        self.assertFalse(any(r["exists"] for r in st["roots"]))

    def test_codex_only(self):
        (self.tmp / "c" / "sessions").mkdir(parents=True)
        st = status_with(self.tmp, [self.tmp / "nope"], self.tmp / "c")
        self.assertEqual(st["empty"], "codex_only")

    def test_claude_root_without_usage(self):
        (self.tmp / "p").mkdir()
        st = status_with(self.tmp, [self.tmp / "p"], self.tmp / "nope")
        self.assertEqual(st["empty"], "no_sessions")

    def test_no_empty_state_once_data_exists(self):
        st = status_with(self.tmp, [self.tmp / "nope"], self.tmp / "nope2", sessions=1)
        self.assertIsNone(st["empty"])

    def test_custom_root_via_env_is_discovered(self):
        (self.tmp / "extra").mkdir()
        with mock.patch.dict("os.environ", {"WHYTOKENS_EXTRA_PROJECTS_DIRS": str(self.tmp / "extra")}):
            st = status_with(self.tmp, [self.tmp / "nope"], self.tmp / "nope2")
        self.assertEqual(st["empty"], "no_sessions")
        self.assertIn(str(self.tmp / "extra"), [r["path"] for r in st["roots"]])

    def test_dashboard_data_on_fresh_empty_db(self):
        db = self.tmp / "e.db"
        conn = scanner.get_db(db)
        scanner.init_db(conn)
        conn.close()
        self.assertNotIn("error", api.get_dashboard_data(db_path=db))


class OddPaths(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def test_spaces_and_unicode_root(self):
        root = self.tmp / "my prøjects é 日本"
        perf_budget.write_corpus(root, 2, 3)
        res = sources.scan_all(db_path=self.tmp / "u.db", sources=[ClaudeSource(projects_dirs=[root])],
                               extensions=[], verbose=False)
        self.assertEqual(res["claude"]["turns"], 6)

    def test_unreadable_file_is_reported_not_recorded(self):
        root = self.tmp / "p"
        perf_budget.write_corpus(root, 2, 3)
        bad = sorted(root.rglob("*.jsonl"))[0]
        real_open = open

        def fake_open(f, *a, **k):
            if str(f) == str(bad):
                raise PermissionError("denied")
            return real_open(f, *a, **k)

        db = self.tmp / "u.db"
        with mock.patch.object(sources, "open", fake_open, create=True):
            sources.PROGRESS["errors"] = []
            res = sources.scan_all(db_path=db, sources=[ClaudeSource(projects_dirs=[root])],
                                   extensions=[], verbose=False)
        self.assertEqual(res["claude"]["turns"], 3)  # the other file still scanned
        self.assertEqual([e["path"] for e in sources.PROGRESS["errors"]], [str(bad)])
        # not marked processed, so it is picked up once readable
        res = sources.scan_all(db_path=db, sources=[ClaudeSource(projects_dirs=[root])],
                               extensions=[], verbose=False)
        self.assertEqual(res["claude"]["turns"], 3)

    def test_occupied_port_gives_actionable_exit(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        with mock.patch.object(api, "DB_PATH", self.tmp / "u.db"), \
             self.assertRaises(SystemExit) as cm:
            api.serve(host="127.0.0.1", port=s.getsockname()[1])
        self.assertIn("--port", str(cm.exception))


class RescanSerialization(unittest.TestCase):
    def test_second_start_while_running_is_refused(self):
        gate = threading.Event()
        entered = threading.Event()

        def slow_scan(**kw):
            entered.set()
            gate.wait(5)
            return {"claude": {k: 0 for k in ("new", "updated", "skipped", "turns", "sessions")}}

        with mock.patch.object(sources, "scan_all", slow_scan):
            self.assertTrue(scanrun.start(Path("x.db")))
            self.assertTrue(entered.wait(5))
            self.assertEqual(scanrun.status()["state"], "running")
            self.assertFalse(scanrun.start(Path("x.db")))  # serialized
            gate.set()
            for _ in range(100):
                if scanrun.status()["state"] == "done":
                    break
                threading.Event().wait(0.05)
        self.assertEqual(scanrun.status()["state"], "done")
        with mock.patch.object(sources, "scan_all", lambda **kw: {}):
            self.assertTrue(scanrun.start(Path("x.db"), wait=True))  # lock released after a run

    def test_scan_error_is_surfaced_not_raised(self):
        with mock.patch.object(sources, "scan_all", side_effect=RuntimeError("boom")):
            scanrun.start(Path("x.db"), wait=True)
        st = scanrun.status()
        self.assertEqual(st["state"], "error")
        self.assertIn("boom", st["error"])


class ProjectsDirOverride(unittest.TestCase):
    def test_status_and_rescan_honor_projects_dir(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            perf_budget.write_corpus(t / "custom", 1, 2)
            seen = []
            real = sources.scan_all
            with mock.patch.object(sources, "scan_all",
                                   lambda **kw: seen.append(kw["projects_dir"]) or real(**kw)),                  mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [t / "nope"]):
                scanrun.start(t / "u.db", t / "custom", wait=True)
                scanrun.start(t / "u.db", wait=True)  # a later rescan keeps the override
                st = api.get_status(t / "u.db")
            scanrun.projects_dir = None
        self.assertEqual(seen, [t / "custom", t / "custom"])
        self.assertEqual([r["path"] for r in st["roots"] if r["source"] == "claude"], [str(t / "custom")])
        self.assertIsNone(st["empty"])

    def test_wait_returns_this_runs_own_result(self):
        with mock.patch.object(sources, "scan_all", lambda **kw: {"claude": {k: 7 for k in
                               ("new", "updated", "skipped", "turns", "sessions")}}):
            st = scanrun.start(Path("x.db"), wait=True)
        self.assertEqual(st["result"]["turns"], 7)


class ScanProgress(unittest.TestCase):
    def test_progress_counts_files(self):
        with tempfile.TemporaryDirectory() as t:
            perf_budget.write_corpus(Path(t) / "p", 3, 2)
            sources.scan_all(db_path=Path(t) / "u.db", sources=[ClaudeSource(projects_dirs=[Path(t) / "p"])],
                             extensions=[], verbose=False)
        self.assertEqual((sources.PROGRESS["source"], sources.PROGRESS["done"], sources.PROGRESS["total"]),
                         ("claude", 3, 3))


class RescanEndpoint(Base):
    def test_rescan_during_a_running_scan_reports_running(self):
        self.assertTrue(scanrun._lock.acquire(blocking=False))
        try:
            conn_status, body = self.req("POST", "/api/rescan", {api.TOKEN_HEADER: TOKEN})
        finally:
            scanrun._lock.release()
        self.assertEqual(conn_status, 200)
        self.assertTrue(json.loads(body)["running"])

    def test_status_endpoint(self):
        status, body = self.req("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("roots", json.loads(body))


class PerformanceBudget(unittest.TestCase):
    def test_synthetic_corpus_within_budget(self):
        m = perf_budget.measure(60, 40)
        self.assertEqual(perf_budget.check(m, scale=3.0), [], m)

    def test_check_catches_over_budget(self):
        over = perf_budget.check({"turns": 100_000, "cold_scan": 999, "warm_scan": 999,
                                  "page_load": 999, "sessions_page": 999, "waste": 999})
        self.assertEqual(len(over), 5)


if __name__ == "__main__":
    unittest.main()
