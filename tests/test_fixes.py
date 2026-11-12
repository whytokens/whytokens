"""test_fixes.py - `fix record` + `verify`.

Falsification contract: a fixture where nothing changed must NOT claim a saving; a real
drop must; under 10 sessions on either side must say "insufficient data". Fixtures go
through the real scanner (JSONL -> scan_all())."""

import contextlib
import http.client
import io
import json
import time
import threading
from http.server import HTTPServer
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import api
import cli
import fixes
from scanner import init_db, CORE_TABLES
from sources import ClaudeSource, scan_all
from tests.test_waste import _main_chain

FIX_DATE = "2026-09-01"


def _session(sid, day, wasteful):
    lines = _main_chain(sid, 10, base=20_000,
                        tool_at={2: [("Bash", {"command": "npm test"})]} if wasteful else {},
                        growth={2: 60_000} if wasteful else {})
    lines = [l.replace("2026-08-20T", f"{day}T") for l in lines]
    return lines


def _build(before, after):
    """before/after: list of wasteful flags, one session per day around FIX_DATE."""
    tmp = tempfile.mkdtemp()
    proj = Path(tmp) / "projects" / "home-dev-acme-app"
    proj.mkdir(parents=True)
    fix = date.fromisoformat(FIX_DATE)
    days = [(fix - timedelta(days=len(before) - i)) for i in range(len(before))] + \
           [(fix + timedelta(days=i)) for i in range(len(after))]
    for i, (day, w) in enumerate(zip(days, before + after)):
        (proj / f"s{i}.jsonl").write_text("\n".join(_session(f"s{i}", day.isoformat(), w)) + "\n", encoding="utf-8")
    db = str(Path(tmp) / "usage.db")
    # scan_all, not the vendored scan(): only the source path writes tool_results (result sizes)
    scan_all(db_path=db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(proj.parent)])])
    return tmp, db


class VerifyTests(unittest.TestCase):
    def _verify(self, before, after, cause="resident_output"):
        tmp, db = _build(before, after)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fid = fixes.record_fix(conn, "acme/app", cause, "trim bash output", FIX_DATE)
        listed = fixes.list_fixes(conn)
        conn.close()
        self.assertEqual([f["id"] for f in listed], [fid])
        return fixes.verify(db, listed[0])

    def test_unchanged_claims_no_saving(self):
        r = self._verify([True] * 12, [True] * 12)
        self.assertEqual(r["status"], "no material change")
        self.assertEqual(r["saving_usd"], 0.0)
        self.assertGreater(r["before"]["pct"], 0.05)  # the waste is really there to be measured

    def test_report_is_asked_for_sub_cent_findings(self):
        # waste.report drops findings under $0.01 by default; verify's share needs them all,
        # or the same share in a smaller session reads as a saving
        with mock.patch.object(fixes.waste, "report", wraps=fixes.waste.report) as rep:
            r = self._verify([True] * 12, [True] * 12)
        self.assertEqual(rep.call_args.kwargs.get("min_usd"), 0)
        self.assertEqual(r["status"], "no material change")

    def test_zero_spend_sessions_are_not_evidence(self):
        self.assertEqual(fixes._side([("a", 0.0)] * 10, {})["sessions"], 0)

    def test_clean_unchanged_claims_no_saving(self):
        r = self._verify([False] * 12, [False] * 12)
        self.assertEqual((r["status"], r["saving_usd"]), ("no material change", 0.0))

    def test_real_drop_is_reduced_with_dollars(self):
        r = self._verify([True] * 12, [False] * 12)
        self.assertEqual(r["status"], "reduced")
        self.assertGreater(r["saving_usd"], 0)
        self.assertAlmostEqual(r["saving_usd"], (r["before"]["pct"] - r["after"]["pct"]) * r["after"]["usd"])
        self.assertGreater(r["before"]["pct"], r["after"]["pct"])
        self.assertIn("Quality is not measured", fixes.format_result(r))

    def test_only_the_recorded_cause_counts(self):
        # the waste is all resident_output; a fix recorded against idle_gap sees none of it
        r = self._verify([True] * 12, [False] * 12, cause="idle_gap")
        self.assertEqual((r["status"], r["saving_usd"]), ("no material change", 0.0))

    def test_increase_not_a_saving(self):
        r = self._verify([False] * 12, [True] * 12)
        self.assertEqual((r["status"], r["saving_usd"]), ("increased", 0.0))

    def test_insufficient_data_either_side(self):
        for b, a in ((9, 12), (12, 9)):
            r = self._verify([True] * b, [False] * a)
            self.assertEqual(r["status"], "insufficient data")
            self.assertEqual(r["saving_usd"], 0.0)
            self.assertIn("at least 10 sessions", r["reason"])
            self.assertIn("insufficient data", fixes.format_result(r))

    def test_record_validation_and_hash(self):
        tmp, db = _build([True], [True])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for args in (("acme/app", "nope", "x"), ("ghost", "idle_gap", "x")):
            with self.assertRaises(ValueError):
                fixes.record_fix(conn, *args)
        with self.assertRaises(ValueError):
            fixes.record_fix(conn, "acme/app", "idle_gap", "x", when="not-a-date")
        fid = fixes.record_fix(conn, "acme/app", "idle_gap", "x", when="20260901")
        self.assertEqual(conn.execute("SELECT fix_date FROM fixes WHERE id=?", (fid,)).fetchone()[0], "2026-09-01")
        cfg = Path(tmp) / "CLAUDE.md"
        cfg.write_text("rules", encoding="utf-8")
        fixes.record_fix(conn, "acme/app", "idle_gap", "x", file_path=cfg)
        self.assertEqual(fixes.list_fixes(conn)[-1]["file_hash"], fixes.file_hash(cfg))
        self.assertIn("fixes", CORE_TABLES)  # schema_change_pending -> backup covers the new table
        conn.close()

    def test_cli_round_trip(self):
        tmp, db = _build([True] * 12, [True] * 12)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = io.StringIO()
        with mock.patch.object(cli, "DB_PATH", Path(db)), contextlib.redirect_stdout(out):
            cli.cmd_fix(["record", "--project", "acme/app", "--cause", "resident_output",
                         "--note", "n", "--date", FIX_DATE])
            cli.cmd_verify()
        self.assertIn("recorded fix #1", out.getvalue())
        self.assertIn("no material change", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class PostFixTests(unittest.TestCase):
    """POST /api/fixes: records through fixes.record_fix behind the POST token."""
    TOKEN = "tok-fix"

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.db = _build([True] * 3, [False] * 3)
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

    def post(self, body, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Content-Type": "application/json"}
        if token is not None:
            h[api.TOKEN_HEADER] = token
        c.request("POST", "/api/fixes", body if isinstance(body, str) else json.dumps(body), h)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT project, cause FROM fixes").fetchall()
        except sqlite3.OperationalError:  # table never created: nothing written
            return []
        finally:
            conn.close()

    def good(self, **kw):
        return {"project": "acme/app", "cause": "resident_output", "note": "trim", **kw}

    def test_same_fix_posted_again_is_not_recorded_twice(self):
        body = self.good(note="dedupe me")
        n = len(self.rows())
        s1, b1 = self.post(body)
        s2, b2 = self.post(body)
        r1, r2 = json.loads(b1), json.loads(b2)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual((r1["duplicate"], r2["duplicate"]), (False, True))
        self.assertEqual(r1["fix"]["id"], r2["fix"]["id"])
        self.assertEqual(len(self.rows()), n + 1)
        self.post(self.good(note="a different rule"))  # another note is a new fix
        self.assertEqual(len(self.rows()), n + 2)

    def test_concurrent_posts_record_once(self):
        body = self.good(note="race")
        n = len(self.rows())
        results = []
        gate = threading.Barrier(16)

        def go():
            gate.wait()
            results.append(api.post_fix(dict(body), db_path=self.db))
        ts = [threading.Thread(target=go) for _ in range(16)]
        real = fixes.record_fix

        def slow(*a, **kw):  # widen the window between the duplicate check and the insert
            time.sleep(0.05)
            return real(*a, **kw)
        with mock.patch.object(fixes, "record_fix", slow):
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(results), 16)
        self.assertEqual(sum(not r["duplicate"] for r in results), 1)
        self.assertEqual(len({r["fix"]["id"] for r in results}), 1)
        self.assertEqual(len(self.rows()), n + 1)

    def test_valid_post_records_and_verifies(self):
        before = len(self.rows())
        status, body = self.post(self.good())
        self.assertEqual(status, 200)
        r = json.loads(body)
        self.assertEqual(r["status"], "insufficient data")
        self.assertEqual(r["fix"]["fix_date"], date.today().isoformat())
        self.assertEqual((r["before"]["sessions"], r["after"]["sessions"]), (6, 0))
        self.assertEqual(len(self.rows()), before + 1)

    def test_rejections_write_nothing(self):
        before = self.rows()
        cases = [(self.good(), None, 403), (self.good(), "wrong", 403),
                 (self.good(cause="nope"), self.TOKEN, 400),
                 (self.good(project="no/such"), self.TOKEN, 400),
                 (self.good(note=5), self.TOKEN, 400), ([1], self.TOKEN, 400),
                 ("{not json", self.TOKEN, 400), ("[" * 5000, self.TOKEN, 400)]
        for body, token, want in cases:
            self.assertEqual(self.post(body, token)[0], want, (body, token))
        self.assertEqual(self.rows(), before)

    def test_short_body_is_rejected(self):
        before = self.rows()
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.putrequest("POST", "/api/fixes")
        c.putheader(api.TOKEN_HEADER, self.TOKEN)
        c.putheader("Content-Length", "500")
        c.endheaders()
        c.send(json.dumps(self.good()).encode())
        c.sock.shutdown(1)  # SHUT_WR: the server sees EOF before 500 bytes
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        self.assertEqual(self.rows(), before)
