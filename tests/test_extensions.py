"""Core extension seam (docs/EXTENSIONS.md) and legacy-DB migration. Temp dirs only."""

import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import api
import config
import extensions
import sources
from scanner import get_db, init_db

EXT_SRC = '''
def _scan(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS ext_t (k TEXT PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO ext_t VALUES ('a')")
    conn.commit()

def describe():
    return {"name": "demo",
            "routes": [("/api/demo", lambda q, db: {"hello": q.get("x", [""])[0]}),
                       ("/api/data", lambda q, db: {"shadowed": True})],
            "tabs": [{"name": "demo", "label": "Demo", "static_dir": "static", "module": "tab.js"}],
            "on_scan": _scan}
'''


def make_ext(root, name="demo", src=EXT_SRC):
    d = Path(root) / name
    (d / "static").mkdir(parents=True)
    (d / "extension.py").write_text(src, encoding="utf-8")
    (d / "static" / "tab.js").write_text("export function render(){}", encoding="utf-8")
    (Path(root) / "secret.txt").write_text("secret", encoding="utf-8")
    return d


class TestLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_valid(self):
        [e] = extensions.load([make_ext(self.root)])
        self.assertEqual(e["name"], "demo")
        self.assertEqual(len(e["routes"]), 2)

    def test_broken_extensions_skipped_with_stderr_line(self):
        bad_syntax = make_ext(self.root, "b1", "def describe(:\n")
        bad_name = make_ext(self.root, "b2", "def describe():\n    return {'name': '../x'}\n")
        bad_route = make_ext(self.root, "b3",
                             "def describe():\n    return {'name': 'r', 'routes': [('/x', len)]}\n")
        missing = self.root / "nope"
        good = make_ext(self.root, "good")
        err = io.StringIO()
        with redirect_stderr(err):
            loaded = extensions.load([bad_syntax, bad_name, bad_route, missing, good])
        self.assertEqual([e["name"] for e in loaded], ["demo"])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 4)

    def test_configured_from_config_json_and_env(self):
        (self.root / "config.json").write_text('{"extensions": ["/a/one"]}', encoding="utf-8")
        with mock.patch.object(config, "DATA_DIR", self.root), \
                mock.patch.dict("os.environ", {"WHYTOKENS_EXTENSIONS": "/b/two"}):
            self.assertEqual([p.name for p in extensions.configured_dirs()], ["one", "two"])


class TestNullConfig(unittest.TestCase):
    def test_extensions_null_in_config_degrades(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "config.json").write_text('{"extensions": null}')
            with mock.patch.object(config, "DATA_DIR", Path(t)):
                self.assertEqual(extensions.configured_dirs(), [])


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exts = extensions.load([make_ext(cls.tmp.name)])
        cls._patches = [
            mock.patch.object(api.DashboardHandler, "extensions", cls.exts),
            mock.patch.object(api, "DB_PATH", Path(cls.tmp.name) / "u.db"),
        ]
        for p in cls._patches:
            p.start()
        cls.server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for p in cls._patches:
            p.stop()
        cls.tmp.cleanup()

    def get(self, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", path, headers={"Host": host} if host else {})  # path sent verbatim
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_extension_route(self):
        self.assertEqual(self.get("/api/demo?x=hi"), (200, b'{"hello": "hi"}'))

    def test_extension_cannot_shadow_core_route(self):
        status, body = self.get("/api/data")
        self.assertNotIn(b"shadowed", body)

    def test_extension_route_behind_guard(self):
        self.assertEqual(self.get("/api/demo", host="evil.example")[0], 403)

    def test_api_extensions_lists_tabs(self):
        status, body = self.get("/api/extensions")
        self.assertEqual(json.loads(body)["tabs"],
                         [{"name": "demo", "label": "Demo", "module_url": "/ext/demo/tab.js"}])

    def test_ext_static_served(self):
        self.assertEqual(self.get("/ext/demo/tab.js")[0], 200)

    def test_ext_traversal_rejected(self):
        for p in ("/ext/demo/../../secret.txt", "/ext/demo/..%2f..%2fsecret.txt",
                  "/ext/demo/../extension.py", "/ext/demo/%2e%2e/%2e%2e/secret.txt",
                  "/ext/demo//etc/passwd", "/ext/nope/tab.js", "/ext/demo/"):
            status, body = self.get(p)
            self.assertEqual(status, 404, p)
            self.assertNotIn(b"secret", body)

    def test_unknown_route_404(self):
        self.assertEqual(self.get("/api/nothing")[0], 404)


class TestScanHook(unittest.TestCase):
    def test_extension_table_survives_rescan(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            exts = extensions.load([make_ext(t)])
            db = t / "x.db"
            (t / "p").mkdir()
            src = [sources.ClaudeSource(projects_dirs=[str(t / "p")])]
            for _ in range(2):
                sources.scan_all(db_path=db, verbose=False, sources=src, extensions=exts)
            c = sqlite3.connect(db)
            c.execute("INSERT INTO ext_t VALUES ('b')")
            c.commit()
            c.close()
            sources.scan_all(db_path=db, verbose=False, sources=src)  # no extensions this time
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("SELECT k FROM ext_t ORDER BY k").fetchall(), [("a",), ("b",)])
            c.close()

    def test_failing_on_scan_does_not_break_scan(self):
        def boom(conn):
            raise RuntimeError("x")
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "p").mkdir()
            err = io.StringIO()
            with redirect_stderr(err):
                sources.scan_all(db_path=Path(t) / "x.db", verbose=False,
                                 sources=[sources.ClaudeSource(projects_dirs=[str(Path(t) / "p")])],
                                 extensions=[{"name": "bad", "on_scan": boom}])
            self.assertIn("bad", err.getvalue())


class TestHookCommit(unittest.TestCase):
    def test_hook_write_persists_when_no_source_changed(self):
        def hook(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS sat (a)")
            conn.execute("INSERT INTO sat VALUES (1)")
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "p").mkdir()
            db = Path(t) / "x.db"
            for _ in range(2):  # second scan changes nothing
                sources.scan_all(db_path=db, verbose=False,
                                 sources=[sources.ClaudeSource(projects_dirs=[str(Path(t) / "p")])],
                                 extensions=[{"name": "h", "on_scan": hook}])
            c = sqlite3.connect(db)
            try:
                self.assertEqual(c.execute("SELECT count(*) FROM sat").fetchone()[0], 2)
            finally:
                c.close()


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.legacy = self.root / "claude" / "usage-profiler.db"
        self.new = self.root / "wt" / "whytokens.db"
        conn = get_db(self.legacy)
        init_db(conn)
        conn.execute("CREATE TABLE sat (a)")
        conn.executemany("INSERT INTO sat VALUES (?)", [(i,) for i in range(50)])
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_and_verifies_counts_keeps_legacy(self):
        self.assertTrue(config.migrate_legacy_db(self.new, self.legacy))
        self.assertTrue(self.legacy.exists())
        self.assertEqual(config._counts(self.new), config._counts(self.legacy))
        self.assertEqual(config._counts(self.new)["sat"], 50)

    def test_noop_when_new_exists_or_legacy_absent(self):
        self.new.parent.mkdir()
        self.new.write_bytes(b"keep")
        self.assertFalse(config.migrate_legacy_db(self.new, self.legacy))
        self.assertEqual(self.new.read_bytes(), b"keep")
        self.assertFalse(config.migrate_legacy_db(self.root / "n2.db", self.root / "absent.db"))

    def test_racing_install_is_not_clobbered_or_deleted(self):
        real = config._copy

        def copy_then_other_run_wins(src, dst):
            real(src, dst)
            self.new.write_bytes(b"other run")  # a concurrent migration installed first
        with mock.patch.object(config, "_copy", copy_then_other_run_wins):
            self.assertFalse(config.migrate_legacy_db(self.new, self.legacy))
        self.assertEqual(self.new.read_bytes(), b"other run")
        self.assertEqual([p.name for p in self.new.parent.iterdir()], ["whytokens.db"])

    def test_short_copy_rejected_and_removed(self):
        real_copy = config._copy

        def short_copy(src, dst):
            real_copy(src, dst)
            c = sqlite3.connect(dst)
            c.execute("DELETE FROM sat WHERE a = 0")
            c.commit()
            c.close()
        with mock.patch.object(config, "_copy", short_copy):
            with self.assertRaisesRegex(RuntimeError, "verify failed"):
                config.migrate_legacy_db(self.new, self.legacy)
        self.assertFalse(self.new.exists())
        self.assertEqual([p.name for p in self.new.parent.glob("*")], [])
        self.assertTrue(self.legacy.exists())

    def test_ensure_db_only_for_default_path(self):
        with mock.patch.object(config, "DB_PATH", self.new), \
                mock.patch.object(config, "LEGACY_DB_PATH", self.legacy), \
                mock.patch.dict("os.environ", {"WHYTOKENS_DB": ""}):
            self.assertFalse(config.ensure_db(self.root / "other.db"))
            self.assertFalse(self.new.exists())
            self.assertTrue(config.ensure_db(self.new))
            self.assertTrue(self.new.exists())


if __name__ == "__main__":
    unittest.main()
