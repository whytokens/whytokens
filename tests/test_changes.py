"""test_changes.py - the Changes screen: status states, the 10-session gate, edit/delete, and
the rendering rules (a preview is never drawn as a result; delete asks first)."""

import contextlib
import http.client
import io
import json
import shutil
import sqlite3
import subprocess
import threading
import unittest
from datetime import date, timedelta
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import api
import cli
import fixes
from scanner import init_db
from tests.test_fixes import FIX_DATE, _build

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
# 12 clean sessions per side, fix on FIX_DATE; "today" for pace maths sits 9 days after it
TODAY = date.fromisoformat(FIX_DATE) + timedelta(days=9)


class _Db(unittest.TestCase):
    def build(self, before, after, cause="resident_output", when=FIX_DATE):
        tmp, db = _build(before, after)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        self.fid = fixes.record_fix(conn, "acme/app", cause, "trim bash output", when)
        self.fix = fixes.list_fixes(conn)[0]
        self.conn, self.db = conn, db
        self.addCleanup(conn.close)

    def view(self, before, after):
        self.build(before, after)
        return fixes.change_view(self.db, self.fix, today=TODAY)


class StateTests(_Db):
    def test_verdict_states_need_the_full_gate(self):
        for b, a, state in (([True] * 12, [False] * 12, "reduced"), ([True] * 12, [True] * 12, "no_change"),
                            ([False] * 12, [True] * 12, "increased")):
            v = self.view(b, a)
            self.assertEqual((v["state"], v["interim"]), (state, False), state)

    def test_waiting_never_a_verdict(self):
        v = self.view([True] * 12, [False] * 3)
        self.assertEqual((v["state"], v["interim"], v["saving_usd"]), ("waiting", True, 0.0))
        self.assertEqual(v["after"]["sessions"], 3)

    def test_short_history_is_not_a_verdict_even_with_enough_after(self):
        v = self.view([True] * 9, [False] * 12)
        self.assertEqual((v["state"], v["interim"], v["saving_usd"]), ("no_history", True, 0.0))

    def test_gate_is_exactly_ten(self):
        self.assertEqual(self.view([True] * 10, [False] * 9)["state"], "waiting")
        self.assertEqual(self.view([True] * 10, [False] * 10)["state"], "reduced")

    def test_eta_from_recent_pace_or_none(self):
        v = self.view([True] * 12, [False] * 3)
        # 15 sessions inside the 28 days before TODAY; 7 more needed
        self.assertEqual(v["eta_days"], -(-7 * 28 // 15))
        far = fixes.change_view(self.db, self.fix, today=TODAY + timedelta(days=200))
        self.assertIsNone(far["eta_days"])

    def test_labels(self):
        v = self.view([True] * 12, [True] * 12)
        self.assertEqual(v["cause_label"], "Tool output kept in context")


class EditDeleteTests(_Db):
    def setUp(self):
        self.build([True] * 2, [False] * 2)

    def test_edit_date_and_note(self):
        fixes.update_fix(self.conn, self.fid, "2026-09-02", "new note")
        f = fixes.list_fixes(self.conn)[0]
        self.assertEqual((f["fix_date"], f["note"]), ("2026-09-02", "new note"))

    def test_rejections_change_nothing(self):
        fixes.record_fix(self.conn, "acme/app", "resident_output", "other", FIX_DATE)
        future = (date.today() + timedelta(days=1)).isoformat()
        for args in ((self.fid, future, None), (self.fid, "not-a-date", None), (self.fid, None, "  "),
                     (self.fid, None, "other"), (999, None, "x")):
            with self.assertRaises(ValueError, msg=args):
                fixes.update_fix(self.conn, *args)
        with self.assertRaises(ValueError):
            fixes.record_fix(self.conn, "acme/app", "idle_gap", "x", future)
        self.assertEqual([(f["fix_date"], f["note"]) for f in fixes.list_fixes(self.conn)],
                         [(FIX_DATE, "trim bash output"), (FIX_DATE, "other")])

    def test_true_is_not_an_id(self):  # JSON true is a Python int 1: it must not address fix #1
        self.assertEqual(self.fid, 1)
        for fn in (api.post_fix_delete, api.post_fix_edit):
            with self.assertRaises(ValueError):
                fn({"id": True, "note": "x"}, db_path=self.db)
        self.assertEqual(len(fixes.list_fixes(self.conn)), 1)

    def test_existing_change_with_future_date_is_still_rejected(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            fixes.record_fix_once(self.conn, "acme/app", "resident_output", "trim bash output", future)

    def test_concurrent_edits_to_one_note_record_it_once(self):
        a = fixes.record_fix(self.conn, "acme/app", "resident_output", "a", FIX_DATE)
        b = fixes.record_fix(self.conn, "acme/app", "resident_output", "b", FIX_DATE)
        real, errors = fixes._update_fix, []

        def slow(*args):
            import time
            time.sleep(0.1)  # widen the window between the duplicate check and the write
            return real(*args)

        def go(fid):
            c = sqlite3.connect(self.db, timeout=10)
            try:
                fixes.update_fix(c, fid, None, "same")
            except ValueError as e:
                errors.append(e)
            finally:
                c.close()
        with mock.patch.object(fixes, "_update_fix", slow):
            ts = [threading.Thread(target=go, args=(i,)) for i in (a, b)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual((len(errors), sum(f["note"] == "same" for f in fixes.list_fixes(self.conn))), (1, 1))

    def test_delete(self):
        fixes.delete_fix(self.conn, self.fid)
        self.assertEqual(fixes.list_fixes(self.conn), [])
        with self.assertRaises(ValueError):
            fixes.delete_fix(self.conn, self.fid)

    def test_cli_edit_and_delete(self):
        with mock.patch.object(cli, "DB_PATH", Path(self.db)), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_fix(["edit", str(self.fid), "--date", "2026-09-03", "--note", "cli note"])
        f = fixes.list_fixes(self.conn)[0]
        self.assertEqual((f["fix_date"], f["note"]), ("2026-09-03", "cli note"))
        with mock.patch.object(cli, "DB_PATH", Path(self.db)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.cmd_fix(["edit", str(self.fid)])
            cli.cmd_fix(["delete", str(self.fid)])
        self.assertEqual(fixes.list_fixes(self.conn), [])


class ApiTests(unittest.TestCase):
    TOKEN = "tok-chg"

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.db = _build([True] * 12, [False] * 3)
        cls._patches = [mock.patch.object(api, "DB_PATH", Path(cls.db)),
                        mock.patch.object(api.DashboardHandler, "token", cls.TOKEN),
                        mock.patch.object(api.DashboardHandler, "allow_any_host", False)]
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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Content-Type": "application/json"}
        if token is not None:
            h[api.TOKEN_HEADER] = token
        c.request(method, path, None if body is None else json.dumps(body), h)
        r = c.getresponse()
        raw = r.read()
        out = (r.status, json.loads(raw) if r.status != 403 else raw)
        c.close()
        return out

    def test_lifecycle(self):
        st, d = self.call("GET", "/api/changes")
        self.assertEqual((st, d["changes"], d["projects"]), (200, [], ["acme/app"]))
        self.assertEqual(d["causes"][0], {"id": "resident_output", "label": "Tool output kept in context"})
        st, r = self.call("POST", "/api/fixes", {"project": "acme/app", "cause": "resident_output", "note": " trim ", "date": FIX_DATE})
        self.assertEqual((st, r["fix"]["fix_date"], r["fix"]["note"]), (200, FIX_DATE, "trim"))
        fid = r["fix"]["id"]
        d = self.call("GET", "/api/changes")[1]
        self.assertEqual((d["changes"][0]["state"], d["changes"][0]["interim"]), ("waiting", True))
        self.assertEqual(self.call("POST", "/api/fixes/edit", {"id": fid, "date": "2026-09-02", "note": "trim 2"})[0], 200)
        self.assertEqual(self.call("GET", "/api/changes")[1]["changes"][0]["fix"]["fix_date"], "2026-09-02")
        self.assertEqual(self.call("POST", "/api/fixes/delete", {"id": fid}, token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/fixes/delete", {"id": fid})[0], 200)
        self.assertEqual(self.call("GET", "/api/changes")[1]["changes"], [])

    def test_rejections(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        good = {"project": "acme/app", "cause": "resident_output", "note": "n"}
        for body in ({**good, "date": future}, {**good, "date": "nope"}, {**good, "note": "  "}, {**good, "date": 5}):
            self.assertEqual(self.call("POST", "/api/fixes", body)[0], 400, body)
        fid = self.call("POST", "/api/fixes", {**good, "date": FIX_DATE})[1]["fix"]["id"]
        for path, body in (("/api/fixes/edit", {"id": str(fid)}), ("/api/fixes/edit", {"id": 4.5}),
                           ("/api/fixes/edit", {"id": 4242, "note": "x"}), ("/api/fixes/delete", {"id": 4242}),
                           ("/api/fixes/delete", {})):
            self.assertEqual(self.call("POST", path, body)[0], 400, (path, body))
        for body in ([], None, "x"):
            self.assertEqual(self.call("POST", "/api/fixes/edit", body)[0], 400, body)
        self.assertEqual(len(self.call("GET", "/api/changes")[1]["changes"]), 1)
        self.assertEqual(self.call("POST", "/api/fixes/delete", {"id": fid})[0], 200)


JS = """
globalThis.document = {createElement: () => ({set textContent(v) { this.t = String(v); }, get innerHTML() {
  return this.t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }}), querySelector: () => null};
const calls = []; const CONFIRM = __CONFIRM__;
globalThis.confirm = () => CONFIRM; globalThis.fetch = async (...a) => { calls.push(a); return {ok: true, json: async () => ({})}; };
const m = await import('file:///__MODULE__');
const c = __CHANGE__;
console.log(JSON.stringify({chip: m.statusChip(c), eta: m.etaLine(c), row: m.rowHtml(c),
  empty: m.emptyHtml(), deleted: await m.deleteChange(7, 'n'), fetches: calls.length}));
"""


def _change(state, before=12, after=12, **kw):
    side = lambda n, p: {"sessions": n, "usd": 10.0, "cause_usd": p * 10, "pct": p}
    c = {"state": state, "interim": state in ("waiting", "no_history"), "min_sessions": 10, "eta_days": None,
         "fix": {"id": 7, "fix_date": FIX_DATE, "project": "acme/app", "cause": "resident_output", "note": "trim"},
         "cause_label": "Tool output kept in context", "before": side(before, 0.30), "after": side(after, 0.10),
         "saving_usd": 2.0 if state == "reduced" else 0.0, "basis": "list rate", "caveat": fixes.CAVEAT, "today": "2026-11-12"}
    c.update(kw)
    return c


@unittest.skipUnless(NODE, "node not installed")
class RenderTests(unittest.TestCase):
    def render(self, change, confirm="true"):
        src = (JS.replace("__CONFIRM__", confirm).replace("__CHANGE__", json.dumps(change))
               .replace("__MODULE__", (ROOT / "static/modules/changes.js").as_posix()))
        r = subprocess.run([NODE, "--input-type=module", "-e", src], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_chips(self):
        want = {"reduced": "Reduced", "no_change": "No material change", "increased": "Increased",
                "no_history": "Not enough history before"}
        for state, text in want.items():
            self.assertEqual(self.render(_change(state))["chip"]["text"], text)
        self.assertEqual(self.render(_change("waiting", after=4))["chip"]["text"], "Waiting: 4 of 10 sessions since")

    def test_eta_lines(self):
        self.assertIn("About 4 more days", self.render(_change("waiting", after=4, eta_days=4))["eta"])
        self.assertIn("No recent sessions", self.render(_change("waiting", after=4))["eta"])
        self.assertEqual(self.render(_change("reduced"))["eta"], "")

    def test_preview_is_greyed_and_labelled_never_a_result(self):
        for state in ("waiting", "no_history"):
            row = self.render(_change(state, after=4))["row"]
            self.assertIn("chg-interim", row)
            self.assertIn("Too early to call", row)
            self.assertNotIn("less over the after period", row)

    def test_verdict_shows_dollars_and_plain_bars(self):
        row = self.render(_change("reduced"))["row"]
        self.assertIn("less over the after period", row)
        self.assertNotIn("chg-interim", row)
        self.assertNotIn("Too early to call", row)
        self.assertIn(fixes.CAVEAT, row)
        self.assertIn("12 sessions", row)

    def test_no_dollars_unless_reduced(self):
        for state in ("no_change", "increased"):
            self.assertNotIn("less over the after period", self.render(_change(state, saving_usd=2.0))["row"])

    def test_empty_state_teaches_and_links_to_waste(self):
        e = self.render(_change("reduced"))["empty"]
        self.assertEqual(e.count("<li>"), 3)
        self.assertIn('href="?tab=waste"', e)

    def test_delete_needs_confirm(self):
        no = self.render(_change("reduced"), confirm="false")
        self.assertEqual((no["deleted"], no["fetches"]), (False, 0))
        yes = self.render(_change("reduced"), confirm="true")
        self.assertEqual((yes["deleted"], yes["fetches"]), (True, 1))


if __name__ == "__main__":
    unittest.main()
