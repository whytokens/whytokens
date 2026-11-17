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

    def test_bound_address_is_the_only_extra_host(self):
        with mock.patch.object(api.DashboardHandler, "allowed_hosts", api.LOOPBACK_HOSTS + ("192.168.1.5",)):
            self.assertEqual(self.req("GET", "/api/pricing", {"Host": "192.168.1.5:8090"})[0], 200)
            self.assertEqual(self.req("GET", "/api/pricing", {"Host": "evil.example"})[0], 403)

    def test_network_shaped_static_path_is_refused_before_it_is_resolved(self):
        import extensions
        ext = {"dir": Path(self._tmp.name), "tabs": [{"static_dir": "p"}]}
        with mock.patch("pathlib.Path.resolve", side_effect=AssertionError("resolved")):
            for rel in ("//192.0.2.1/share/x", "/192.0.2.1/x", "\\\\192.0.2.1\\share\\x", "a\\b.js", "C:x.js", "x.js:s"):
                self.assertIsNone(api.resolve_static(rel), rel)
                self.assertIsNone(extensions.static_file(ext, rel), rel)
        self.assertEqual(self.req("GET", "/static///192.0.2.1/share/x")[0], 404)
        self.assertEqual(self.req("GET", "/static/modules/format.js")[0], 200)

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
        posts = [c for c in js.split('export async function ') if "method: 'POST'" in c]
        self.assertTrue(posts)
        for c in posts:   # every state-changing call sends it, not just one of them
            self.assertIn(api.TOKEN_HEADER, c, c.split('(')[0])


class TestLoopbackBind(unittest.TestCase):
    def test_non_loopback_requires_unsafe_host(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with mock.patch.object(api, "_Server") as srv:   # never bind for real
                srv.return_value.serve_forever.side_effect = KeyboardInterrupt
                with self.assertRaises(SystemExit):
                    api.serve(host=host, port=0)

    def test_unsafe_host_flag_allows_it(self):
        with mock.patch.object(api, "_Server") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            with mock.patch.multiple(api.DashboardHandler, token=None, allow_any_host=False):
                api.serve(host="0.0.0.0", port=0, unsafe_host=True)
                self.assertTrue(api.DashboardHandler.allow_any_host)
            srv.assert_called_once()

    def test_unsafe_host_on_one_address_still_checks_host(self):
        for host in ("127.0.0.1", "192.168.1.5"):
            with mock.patch.object(api, "_Server") as srv:
                srv.return_value.serve_forever.side_effect = KeyboardInterrupt
                with mock.patch.multiple(api.DashboardHandler, token=None, allow_any_host=True,
                                         allowed_hosts=api.LOOPBACK_HOSTS):
                    api.serve(host=host, port=0, unsafe_host=True)
                    self.assertFalse(api.DashboardHandler.allow_any_host, host)
                    self.assertIn(host, api.DashboardHandler.allowed_hosts)
                    self.assertNotIn("evil.example", api.DashboardHandler.allowed_hosts)

    def test_serve_generates_a_token(self):
        with mock.patch.object(api, "_Server") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            with mock.patch.multiple(api.DashboardHandler, token=None, allow_any_host=False):
                api.serve(host="localhost", port=0)
                self.assertGreaterEqual(len(api.DashboardHandler.token), 32)

    def test_cli_exposes_the_flag(self):
        self.assertIn("--unsafe-host", (Path(api.__file__).parent / "cli.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
class TestRowsEscapeTranscriptNames(unittest.TestCase):
    """Project, branch, agent type and session id come from transcripts. Runs the real row
    renderers with a hostile name and reads the HTML they produce."""

    def test_hostile_names_never_reach_the_html(self):
        import subprocess
        static = Path(api.__file__).parent / "static" / "modules"
        app = (static / "app.js").read_text(encoding="utf-8")
        fmt = (static / "format.js").read_text(encoding="utf-8")
        cut = lambda src, a, b: src[src.index(a):src.index(b, src.index(a))]
        code = r"""
const elements = {};
const document = {
  createElement() { return {textContent: '', get innerHTML() {
    return this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }}; },
  getElementById(id) { return elements[id] ||= {innerHTML: ''}; }
};
const fmt = String, fmtCost = String, fmtPct = String, fmtWall = String;
const sortProjectBranch = r => r, branchLimit = 10, renderTableToggle = () => {};
""" + cut(fmt, "export function esc(", "/**").replace("export ", "") \
            + cut(app, "function renderSubagentDispatchTable(", "// ── Tool call detail") \
            + cut(app, "function renderProjectBranchCostTable(", "// ── CSV Export") + r"""
const assert = require('node:assert/strict');
const BAD = `"><img src=x onerror=alert(1)>`;
const row = {agent_type: BAD, project: BAD, branch: BAD, session_id: BAD, full_session_id: BAD};
renderSubagentDispatchTable([row]);
renderSessionSignalsTable([row]);
renderProjectBranchCostTable([row]);
const SAFE = '&quot;&gt;&lt;img';
for (const [id, want] of [['subagent-dispatch-body', 5], ['session-signals-body', 4], ['project-branch-cost-body', 3]]) {
  const html = elements[id].innerHTML;
  assert.ok(!html.includes('<img'), id + ' lets a tag through');
  assert.ok(!html.includes('"&gt;&lt;img'), id + ' lets a quote close its attribute');
  assert.equal(html.split(SAFE).length - 1, want, id + ' escaped count');
}
"""
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
