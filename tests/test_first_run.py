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

    def test_stray_legacy_db_is_not_imported(self):
        # a first run must start empty, not copy the old private profiler DB.
        # Subprocess: config reads the home dir at import time, and under unittest it
        # deliberately points elsewhere, so only a fresh interpreter sees the real path.
        import os
        import subprocess
        legacy = self.tmp / ".claude" / "usage-profiler.db"
        legacy.parent.mkdir()
        conn = scanner.get_db(legacy)
        scanner.init_db(conn)
        conn.close()
        env = {k: v for k, v in os.environ.items() if not k.startswith("WHYTOKENS_")}
        env.update(HOME=str(self.tmp), USERPROFILE=str(self.tmp))
        r = subprocess.run([sys.executable, "-c", "import cli; cli.require_db()"],
                           cwd=Path(__file__).resolve().parent.parent, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Database not found", r.stdout)
        self.assertFalse((self.tmp / ".whytokens" / "whytokens.db").exists())

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

    def test_port_still_held_by_a_stopping_instance_is_waited_for(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        threading.Timer(0.4, s.close).start()  # the old dashboard finishes exiting a moment after the new one starts

        class Stop(Exception):
            pass
        with mock.patch.object(api, "DB_PATH", self.tmp / "u.db"), mock.patch("builtins.print"), \
             mock.patch.object(api._Server, "serve_forever", side_effect=Stop), self.assertRaises(Stop):
            api.serve(host="127.0.0.1", port=s.getsockname()[1])  # exited "Cannot listen" at once before

    def test_occupied_port_gives_actionable_exit(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        with mock.patch.object(api, "DB_PATH", self.tmp / "u.db"), mock.patch.object(api, "BIND_WAIT_S", 0.3), \
             self.assertRaises(SystemExit) as cm:
            api.serve(host="127.0.0.1", port=s.getsockname()[1])
        self.assertIn("--port", str(cm.exception))

    def test_serve_without_scan_migrates_an_older_db(self):
        from scanner import get_db, init_db
        db = self.tmp / "old.db"
        conn = get_db(db)
        init_db(conn)
        conn.execute("ALTER TABLE sessions DROP COLUMN shape")  # a DB from before the column existed
        conn.commit()
        conn.close()

        class FakeServer:
            def __init__(self, *a): pass
            def serve_forever(self): raise KeyboardInterrupt

        with mock.patch.object(api, "DB_PATH", db), \
             mock.patch.object(api, "_Server", FakeServer), \
             mock.patch("builtins.print"):
            api.serve(host="127.0.0.1", port=0)
            api.get_shapes(db)  # raised "no such column: shape" before serve migrated


class ScanBannerSaysWhatIsHappening(unittest.TestCase):
    def test_status_reports_the_stage_of_the_scan(self):
        seen = []

        class Src:
            name = "fake"
            def backfill_topics(self, conn): seen.append(("before the walk", scanrun.status()["phase"]))
            def discover_files(self): return []
            def sync_agents(self, conn): seen.append(("after the walk", scanrun.status()["phase"]))
        tmp = Path(tempfile.mkdtemp())
        sources.scan_all(db_path=tmp / "u.db", verbose=False, sources=[Src()])
        self.assertEqual(seen, [("before the walk", "prepare"), ("after the walk", "finish")])
        self.assertEqual(sources.PROGRESS["phase"], "finish")
        with mock.patch.object(sources, "scan_all", return_value={}):
            scanrun.start(tmp / "u.db", wait=True)
        self.assertIsNone(sources.PROGRESS["phase"])  # a new run starts with no stale stage

    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_banner_line_per_stage(self):
        import subprocess
        mod = (Path(__file__).resolve().parent.parent / "static" / "modules" / "api.js").as_uri()
        code = f"""import {{ scanLine }} from {json.dumps(mod)};
        const at = 1000, sc = o => ({{ state: 'running', started: at, source: 'claude', done: 0, total: 0, ...o }});
        console.log(JSON.stringify([
          scanLine(sc({{ phase: 'prepare' }}), at + 75),
          scanLine(sc({{ phase: 'walk', done: 1200, total: 8327 }}), at + 9),
          scanLine(sc({{ phase: 'walk', source: 'coordinator' }}), at + 9),
          scanLine(sc({{ phase: 'finish', source: 'coordinator' }}), at + 9)]));"""
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        prepare, walk, empty, finish = json.loads(out.stdout)
        self.assertIn("1m 15s", prepare)
        self.assertIn("re-read once", prepare)
        self.assertIn("1,200 of 8,327", walk)
        self.assertIn("9s", walk)
        for line in (prepare, empty, finish):
            self.assertNotIn("0 of 0", line)  # a count that says nothing is not shown
        self.assertIn("session totals", finish)

    def test_page_does_not_reload_all_data_every_second_while_scanning(self):
        src = (Path(__file__).resolve().parent.parent / "static" / "modules" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("await refreshStatus(); await loadData(); }, 1000)", src)
        self.assertIn("SCAN_RELOAD_MS = 15000", src)
        self.assertIn("else if (wasScanning) loadData();", src)  # the final numbers, once, when the scan ends


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
                                  "page_load": 999, "sessions_page": 999, "waste": 999, "shapes": 999})
        self.assertEqual(len(over), 6)


class MistypedCommands(unittest.TestCase):
    def exit_code(self, *argv):
        import cli, contextlib, io
        with mock.patch.object(sys, "argv", ["whytokens", *argv]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                cli.main()
        return e.exception.code

    def test_unknown_command_is_an_error_and_help_is_not(self):
        self.assertEqual(self.exit_code("scna"), 2)
        for ok in ([], ["--help"], ["-h"], ["help"]):
            self.assertEqual(self.exit_code(*ok), 0, ok)

    def test_export_without_json_is_an_error(self):
        import cli, contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            cli.cmd_export(as_json=False)
        self.assertEqual(e.exception.code, 2)

    def test_a_flag_is_never_taken_as_another_flags_value(self):
        import cli, contextlib, io
        self.assertEqual(cli.parse_named_arg(["--note", "trim output", "--date", "2026-10-01"], "--note"), "trim output")
        self.assertEqual(cli.parse_named_arg(["--offset", "-5"], "--offset"), "-5")   # one dash is a value
        self.assertIsNone(cli.parse_named_arg(["--date", "2026-10-01"], "--note"))
        for bad in (["--note", "--date", "2026-10-01"], ["--date", "2026-10-01", "--note"]):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                cli.parse_named_arg(bad, "--note")
            self.assertEqual(e.exception.code, 2, bad)


@unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
class PageOddities(unittest.TestCase):
    APP = (Path(__file__).resolve().parent.parent / "static" / "modules" / "app.js").read_text(encoding="utf-8")

    def node(self, code):
        import subprocess
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def cut(self, a, b):
        return self.APP[self.APP.index(a):self.APP.index(b, self.APP.index(a))]

    def test_page_load_rescans_after_showing_the_db(self):
        init = self.cut("async function init(", "\ninit();")
        self.assertIn("loadData().then(triggerRescan);", init)  # F5 picks up new turns

    def test_rescan_checks_status_while_the_scan_runs(self):
        # F5 used to check status before the POST and again only after the scan ended,
        # so the "scanning" banner never showed for the scan F5 itself started.
        self.node("let scanning = false;\n" + self.cut("async function triggerRescan(", "// ── Data loading") + r"""
const assert = require('node:assert/strict');
const btn = {disabled: false, textContent: ''};
globalThis.document = {getElementById: () => btn};
const timers = []; globalThis.setTimeout = (f, ms) => timers.push([f, ms]);
let finish; const postRescan = () => new Promise(r => { finish = r; });
let checks = 0; const refreshStatus = () => { checks++; };
const loadData = async () => {};
(async () => {
  const done = triggerRescan();
  timers.filter(([, ms]) => ms < 3000).forEach(([f]) => f());   // fire the early check, POST still pending
  assert.equal(checks, 1, 'status checked while the scan runs');
  finish({new: 0, updated: 0}); await done;
  scanning = true; timers.length = 0; btn.disabled = false;     // a poll loop is already running
  triggerRescan();
  timers.filter(([, ms]) => ms < 3000).forEach(([f]) => f());
  assert.equal(checks, 2, 'no second poll loop');  // only the post-scan check, not the early one
})().catch(e => { console.error(e); process.exit(1); });
""")

    def test_csv_cells_cannot_start_a_formula(self):
        self.node(self.cut("function csvField(", "function csvTimestamp(") + r"""
const assert = require('node:assert/strict');
for (const bad of ['=1+1', '+cmd|calc!A0', '-2+3', '@SUM(A1)', '\tx'])
  assert.equal(csvField(bad).replace(/^"/, '')[0], "'", bad);
assert.equal(csvField('+cmd|calc!A0,"x" y'), `"'+cmd|calc!A0,""x"" y"`);   // still quoted as CSV
assert.equal(csvField('safe\r=1+1'), '"safe\r=1+1"');      // a carriage return stays inside one quoted cell
assert.equal(csvField('\r=1+1'), `"'\r=1+1"`);
for (const ok of [-5, '-0.25', '+3', 12, 'main', 'feat/a-b', ''])
  assert.equal(csvField(ok), String(ok), String(ok));
""")

    def test_a_late_response_does_not_replace_a_newer_one(self):
        # Local refresh in flight, then a click on UTC whose response lands first
        self.node(r"""
const waiting = [];
const fetchData = tz => new Promise(done => waiting.push(() => done({tz, all_models: ['m']})));
let rawData = {all_models: ['m']}, displayTZ = 'local', wastePayload, shapesPayload, shapesFetch, wasteGen = 0, draws = 0;
const sessionFindings = new Map(), sessionLanes = new Map();
const document = {body: {classList: {remove() {}}}};
const renderMeta = () => {}, applyFilter = () => { draws++; };
""" + self.cut("async function loadData(", "let autoRefreshTimer") + r"""
const assert = require('node:assert/strict');
(async () => {
  const older = loadData();
  displayTZ = 'utc';
  const newer = loadData();
  waiting[1](); await newer;
  waiting[0](); await older;
  assert.equal(rawData.tz, 'utc');
  assert.equal(draws, 1);
  const alone = loadData(); waiting[2](); await alone;   // a request with nothing after it still lands
  assert.equal(draws, 2);
})().catch(e => { console.error(e); process.exit(1); });
""")


if __name__ == "__main__":
    unittest.main()


class SuiteStaysOffRealData(unittest.TestCase):
    def _config_path(self, **env):
        import os
        import subprocess
        import sys
        e = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "WHYTOKENS_TEST_CODEX_HOME")}
        e.update(env)
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run([sys.executable, "-c", "import unittest, config; print(config.CODEX_PATH)"],
                             cwd=root, env=e, capture_output=True, text=True, timeout=60)
        return Path(out.stdout.strip())

    def test_inherited_codex_home_is_ignored_under_unittest(self):
        # a default-sources scan in any test would otherwise decode every real rollout (minutes, and real data)
        fake = Path.home() / "an-exported-real-codex-home"
        got = self._config_path(CODEX_HOME=str(fake))
        self.assertNotEqual(got, fake)
        self.assertNotEqual(got, Path.home() / ".codex")

    def test_a_test_can_still_point_it_explicitly(self):
        fake = Path.home() / "explicit-test-home"
        self.assertEqual(self._config_path(WHYTOKENS_TEST_CODEX_HOME=str(fake)), fake)
