"""One scan per DB across processes, a column migration that survives a concurrent one, and a
dashboard port that a second instance cannot share."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api
import cli
import scanner
import sources

ROOT = Path(__file__).resolve().parent.parent
HOLD = ("import sys, sources\n"
        "with sources._scan_lock(sys.argv[1]):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.read()\n")


class TestScanLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "usage.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def holder(self):
        p = subprocess.Popen([sys.executable, "-c", HOLD, self.db], cwd=ROOT, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        p.stdout.close()
        return p

    def test_scan_is_refused_while_another_process_scans_the_same_db(self):
        self.holder()
        with self.assertRaises(sources.ScanBusy):
            sources.scan_all(db_path=self.db, verbose=False, sources=[])
        self.assertFalse(os.path.exists(self.db))  # refused before the DB was opened or migrated

    def test_cli_scan_reports_the_other_scan_instead_of_a_traceback(self):
        self.holder()
        with mock.patch.object(cli, "DB_PATH", self.db), self.assertRaises(SystemExit) as e:
            cli.cmd_scan()
        self.assertIn("another whytokens process is scanning", str(e.exception))

    def test_first_scan_creates_a_missing_data_directory(self):
        import sources
        fresh = str(Path(self.tmp) / "not" / "yet" / "usage.db")
        with sources._scan_lock(fresh):
            self.assertTrue(Path(fresh).parent.is_dir())

    def test_lock_of_a_killed_process_does_not_block_the_next_scan(self):
        p = self.holder()
        p.kill()
        p.wait()
        sources.scan_all(db_path=self.db, verbose=False, sources=[])
        sources.scan_all(db_path=self.db, verbose=False, sources=[])  # and it is released after a normal scan

    def test_migration_tolerates_a_column_another_process_just_added(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        scanner.init_db(conn)
        with mock.patch.object(scanner, "_table_cols", return_value=set()):  # the column list read before the other ALTER
            scanner.init_db(conn)
        with mock.patch.object(scanner, "COLUMN_MIGRATIONS", {"turns": {"second_key": "INTEGER PRIMARY KEY"}}):
            with self.assertRaises(sqlite3.OperationalError):  # any other failure still stops the migration
                scanner.init_db(conn)


class TestBackupAndCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "old.db"
        conn = scanner.get_db(self.db)
        scanner.init_db(conn)
        conn.execute("ALTER TABLE sessions DROP COLUMN shape")  # a DB from before the column existed
        conn.commit()
        conn.close()

    def migrate(self):
        conn = scanner.get_db(self.db)
        try:
            scanner.init_db(conn)
        finally:
            conn.close()

    def baks(self):
        return sorted(p.name for p in self.tmp.iterdir() if ".bak-" in p.name)

    def test_leftover_tmp_from_a_killed_run_does_not_block_the_migration(self):
        from datetime import datetime
        bak = f"{self.db}.bak-{datetime.now():%Y%m%d}"
        for name in (f"{bak}.tmp", f"{bak}.{os.getpid()}.tmp"):
            Path(name).write_bytes(b"not a database")
        self.migrate()
        self.assertGreater(Path(bak).stat().st_size, 0)
        sqlite3.connect(bak).execute("SELECT COUNT(*) FROM sessions").fetchone()  # a real copy
        self.assertNotIn(f"{Path(bak).name}.{os.getpid()}.tmp", self.baks())

    def test_empty_same_day_backup_is_replaced_by_a_real_one(self):
        from datetime import datetime
        bak = Path(f"{self.db}.bak-{datetime.now():%Y%m%d}")
        bak.write_bytes(b"")
        self.migrate()
        self.assertGreater(bak.stat().st_size, 0)

    def test_backup_published_by_another_process_first_is_never_replaced(self):
        from datetime import datetime
        bak = Path(f"{self.db}.bak-{datetime.now():%Y%m%d}")
        real = os.link

        def other_first(src, dst):  # the other process published its pre-migration copy while this one copied
            Path(dst).write_bytes(b"THE FIRST BACKUP")
            return real(src, dst)
        with mock.patch.object(scanner.os, "link", other_first):
            self.migrate()
        self.assertEqual(bak.read_bytes(), b"THE FIRST BACKUP")
        self.assertEqual(self.baks(), [bak.name])  # and this process's temporary copy is gone

    def test_backup_lands_on_a_filesystem_without_hard_links(self):
        with mock.patch.object(scanner.os, "link", side_effect=OSError(1, "not supported")):
            self.migrate()
        (bak,) = [p for p in self.tmp.iterdir() if ".bak-" in p.name]
        sqlite3.connect(bak).execute("SELECT COUNT(*) FROM sessions").fetchone()  # a real copy

    def test_losing_the_replace_race_to_another_process_is_not_an_error(self):
        real = os.replace

        def lose(src, dst):  # the other process put its copy there first and still holds it
            real(src, str(dst) + ".other")
            shutil.copy(str(dst) + ".other", dst)
            raise PermissionError(32, "in use")
        with mock.patch.object(scanner.os, "link", side_effect=OSError(1, "not supported")), \
             mock.patch.object(scanner.os, "replace", lose):
            self.migrate()
        with mock.patch.object(scanner.os, "link", side_effect=OSError(1, "not supported")), \
             mock.patch.object(scanner.os, "replace", side_effect=PermissionError(32, "in use")):
            for p in self.tmp.iterdir():
                if ".bak-" in p.name:
                    p.unlink()
            conn = scanner.get_db(self.db)
            self.addCleanup(conn.close)
            conn.execute("ALTER TABLE sessions DROP COLUMN shape")
            conn.commit()
            with self.assertRaises(OSError):  # no backup landed at all: the migration must stop
                scanner.init_db(conn)

    def test_read_commands_migrate_an_older_db_before_reading_it(self):
        for argv in (["shapes"], ["llm", "label", "no-such-session"]):
            with mock.patch.object(cli, "DB_PATH", self.db), mock.patch.object(sys, "argv", ["whytokens", *argv]), \
                 mock.patch("builtins.print"):
                try:
                    cli.main()
                except SystemExit:
                    pass  # an unknown session may exit non-zero; a missing column must not be the reason
            conn = sqlite3.connect(self.db)
            self.assertIn("shape", [r[1] for r in conn.execute("PRAGMA table_info(sessions)")], argv)
            conn.execute("ALTER TABLE sessions DROP COLUMN shape")
            conn.commit()
            conn.close()


class TestWritersBesideTheScan(unittest.TestCase):
    """The dashboard writes small things while a scan runs (a recorded change, the adapter's table).
    Neither side may fail the other for it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "u.db"
        conn = scanner.get_db(self.db)
        scanner.init_db(conn)
        conn.execute("INSERT INTO sessions (session_id, project_name, first_timestamp) VALUES ('s1', 'p', '2026-11-01T00:00:00Z')")
        conn.execute("INSERT INTO turns (session_id, timestamp, message_id, tool_calls) VALUES "
                     "('s1', '2026-11-01T00:00:01Z', 'm1', '[{\"name\": \"Edit\", \"input\": {}}]')")
        conn.commit()
        conn.close()

    def test_shape_update_survives_a_write_committed_while_it_reads(self):
        import shape
        conn = scanner.get_db(self.db)
        self.addCleanup(conn.close)
        other = sqlite3.connect(self.db, timeout=0)
        self.addCleanup(other.close)
        seen, real = [], shape._names

        def names(blob):  # runs between the scan's read of turns and its UPDATE of sessions
            if not seen:
                try:
                    other.execute("INSERT INTO fixes (fix_date, project, cause) VALUES ('2026-11-01', 'p', 'idle_gap')")
                    other.commit()
                    seen.append("committed")
                except sqlite3.OperationalError:
                    other.rollback()
                    seen.append("made to wait")
            return real(blob)
        with mock.patch.object(shape, "_names", names):
            self.assertEqual(shape.sync_shapes(conn), 1)  # raised "database is locked" when the other write got in
        self.assertEqual(seen, ["made to wait"])
        self.assertIsNotNone(conn.execute("SELECT shape FROM sessions WHERE session_id = 's1'").fetchone()[0])



class TestWasteComputedOnceForConcurrentRequests(unittest.TestCase):
    def test_requests_arriving_together_share_one_report(self):
        import threading
        import time
        import waste
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "u.db"
        conn = scanner.get_db(db)
        scanner.init_db(conn)
        conn.close()
        calls = []

        def slow(*a, **kw):  # long enough for every request to arrive while the first still computes
            calls.append(1)
            time.sleep(0.3)
            return []
        with mock.patch.object(waste, "report", slow), mock.patch.object(api, "_waste_cache", None):
            threads = [threading.Thread(target=api.get_waste, args=(db,)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)


class TestVerifyInputsCachedPerDbState(unittest.TestCase):
    def test_changes_and_fixes_reuse_one_gather_until_the_db_changes(self):
        import fixes
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "u.db"
        conn = scanner.get_db(db)
        scanner.init_db(conn)
        conn.execute("INSERT INTO sessions (session_id, project_name, first_timestamp) VALUES ('s1', 'p', '2026-11-01T00:00:00Z')")
        fixes.record_fix(conn, "p", "idle_gap", "n", "2026-11-02")
        conn.commit()
        conn.close()
        real, calls = fixes._gather_all, []

        def counted(path):
            calls.append(1)
            return real(path)
        with mock.patch.object(fixes, "_gather_all", counted), mock.patch.object(api, "_gather_cache", None):
            first = api.get_changes(db)
            api.get_fixes(db)
            self.assertEqual(api.get_changes(db), first)
            self.assertEqual(len(calls), 1)  # Changes tab, Trace verify slot, Changes again: one pass
            conn = sqlite3.connect(db)
            fixes.record_fix(conn, "p", "resident_output", "m", "2026-11-03")
            conn.commit()
            conn.close()
            self.assertEqual(len(api.get_changes(db)["changes"]), 2)
            self.assertEqual(len(calls), 2)  # the DB changed: measured again


class TestPort(unittest.TestCase):
    def test_backlog_holds_a_page_load_while_a_request_computes(self):
        self.assertGreaterEqual(api._Server.request_queue_size, 64)  # the default 5 refused connections on a busy server


    def test_second_dashboard_cannot_listen_on_a_served_port(self):
        first = api._Server(("127.0.0.1", 0), api.DashboardHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            api._Server(("127.0.0.1", first.server_address[1]), api.DashboardHandler).server_close()


if __name__ == "__main__":
    unittest.main()
