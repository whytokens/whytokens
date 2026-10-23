"""Local-server hardening: loopback bind, Host and Origin
checks, startup token on POST. scripts/falsify_data_policy.py breaks each rule."""

import http.client
import re
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import api
import scanner
import sources
import sources.codex as codex_mod
import config

TOKEN = "tok-123"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "p").mkdir()
        (tmp / "c").mkdir()
        cls._patches = [
            mock.patch.object(api, "DB_PATH", tmp / "u.db"),
            mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [tmp / "p"]),
            mock.patch.object(codex_mod, "CODEX_PATH", tmp / "c"),
            mock.patch.object(api.DashboardHandler, "token", TOKEN),
            mock.patch.object(api.DashboardHandler, "allow_any_host", False),
        ]
        for p in cls._patches:
            p.start()
        cls.server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for p in cls._patches:
            p.stop()
        cls._tmp.cleanup()

    def req(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest(method, path, skip_host=True)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        for k, v in h.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body


class TestHostAndOrigin(Base):
    def test_loopback_host_ok(self):
        for host in (f"127.0.0.1:{9}", "localhost", "localhost:8090", "[::1]:8090"):
            self.assertEqual(self.req("GET", "/api/pricing", {"Host": host})[0], 200, host)

    def test_foreign_host_refused_dns_rebinding(self):
        for host in ("evil.example", "evil.example:8090", "127.0.0.1.evil.example", "192.168.1.5:8090", None):
            self.assertEqual(self.req("GET", "/api/data", {"Host": host})[0], 403, host)

    def test_foreign_origin_refused_even_on_get(self):
        self.assertEqual(self.req("GET", "/api/data", {"Origin": "http://evil.example"})[0], 403)

    def test_same_origin_get_ok(self):
        self.assertEqual(
            self.req("GET", "/api/pricing", {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_cross_site_fetch_metadata_refused(self):
        self.assertEqual(self.req("GET", "/api/data", {"Sec-Fetch-Site": "cross-site"})[0], 403)


class TestPostToken(Base):
    def test_rescan_without_token_refused(self):
        self.assertEqual(self.req("POST", "/api/rescan")[0], 403)

    def test_rescan_wrong_token_refused(self):
        self.assertEqual(self.req("POST", "/api/rescan", {api.TOKEN_HEADER: "nope"})[0], 403)

    def test_rescan_foreign_origin_refused_even_with_token(self):
        h = {api.TOKEN_HEADER: TOKEN, "Origin": "http://evil.example"}
        self.assertEqual(self.req("POST", "/api/rescan", h)[0], 403)

    def test_rescan_foreign_host_refused_even_with_token(self):
        h = {api.TOKEN_HEADER: TOKEN, "Host": "evil.example"}
        self.assertEqual(self.req("POST", "/api/rescan", h)[0], 403)

    def test_rescan_with_token_ok(self):
        self.assertEqual(self.req("POST", "/api/rescan", {api.TOKEN_HEADER: TOKEN})[0], 200)

    def test_no_server_token_fails_closed(self):
        with mock.patch.object(api.DashboardHandler, "token", None):
            self.assertEqual(self.req("POST", "/api/rescan", {api.TOKEN_HEADER: ""})[0], 403)

    def test_index_carries_the_token_the_page_sends(self):
        status, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        m = re.search(rb'<meta name="whytokens-token" content="([^"]*)">', body)
        self.assertEqual(m.group(1).decode(), TOKEN)
        js = (Path(api.__file__).parent / "static" / "modules" / "api.js").read_text(encoding="utf-8")
        self.assertIn("whytokens-token", js)
        self.assertIn(api.TOKEN_HEADER, js)


class TestLoopbackBind(unittest.TestCase):
    def test_non_loopback_requires_unsafe_host(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with mock.patch.object(api, "ThreadingHTTPServer") as srv:   # never bind for real
                srv.return_value.serve_forever.side_effect = KeyboardInterrupt
                with self.assertRaises(SystemExit):
                    api.serve(host=host, port=0)

    def test_unsafe_host_flag_allows_it(self):
        with mock.patch.object(api, "ThreadingHTTPServer") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            with mock.patch.multiple(api.DashboardHandler, token=None, allow_any_host=False):
                api.serve(host="0.0.0.0", port=0, unsafe_host=True)
                self.assertTrue(api.DashboardHandler.allow_any_host)
            srv.assert_called_once()

    def test_serve_generates_a_token(self):
        with mock.patch.object(api, "ThreadingHTTPServer") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            with mock.patch.multiple(api.DashboardHandler, token=None, allow_any_host=False):
                api.serve(host="localhost", port=0)
                self.assertGreaterEqual(len(api.DashboardHandler.token), 32)

    def test_cli_exposes_the_flag(self):
        self.assertIn("--unsafe-host", (Path(api.__file__).parent / "cli.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
