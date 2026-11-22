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

    def test_short_baselines_agree_leaves_verdict(self):
        r = self._verify([True] * 12, [False] * 12)
        self.assertEqual(r["status"], "reduced")
        bl = r["baselines"]
        self.assertFalse(bl["disagree"])
        self.assertEqual([w["verdict"] for w in bl["windows"]], ["skipped", "agrees", "agrees"])

    def test_seven_day_reversal_is_flagged_not_vetoed(self):
        r = self._verify([True] * 10 + [False] * 7, [True] * 3 + [False] * 9)
        self.assertEqual(r["status"], "reduced")  # the full-baseline verdict itself is untouched
        bl = r["baselines"]
        self.assertTrue(bl["disagree"])
        self.assertEqual([w["verdict"] for w in bl["windows"]], ["skipped", "disagrees", "disagrees"])

    def test_window_under_floor_is_shown_and_cannot_veto(self):
        # only the last 3 days reverse, and they would flip the result; 3 sessions is under the floor of 5
        r = self._verify([True] * 14 + [False] * 3, [True] * 3 + [False] * 9)
        self.assertEqual(r["baselines"]["windows"][0]["verdict"], "skipped")
        self.assertIn("needs 5", r["baselines"]["windows"][0]["reason"])
        self.assertFalse(r["baselines"]["disagree"])  # the skipped window would have flipped; it must not count

    def test_counter_metrics_all_shown_when_cost_per_prompt_rises(self):
        rows = [(f"b{i}", "2026-08-%02d" % (i + 1), None) for i in range(12)] +                [(f"a{i}", "2026-09-%02d" % (i + 1), None) for i in range(12)]
        totals = {s: 10.0 for s, _, _ in rows}
        cause = {s: 3.0 if s[0] == "b" else 1.0 for s, _, _ in rows}
        # (prompts, compactions, cache-read tokens, context tokens)
        counters = {s: (5, 0, 50, 100) if s[0] == "b" else (2, 1, 90, 100) for s, _, _ in rows}
        r = fixes.verify(None, {"id": 1, "note": "n", "fix_date": FIX_DATE, "cause": "resident_output", "project": "p"},
                         (totals, rows, cause, counters))
        self.assertEqual(r["status"], "reduced")
        got = {c["id"]: c for c in r["counters"]}
        self.assertEqual(sorted(got), ["compactions_per_session", "prompts_per_session", "reread_share", "usd_per_prompt"])
        self.assertEqual((got["usd_per_prompt"]["before"], got["usd_per_prompt"]["after"]), (2.0, 5.0))
        self.assertEqual({k: v["direction"] for k, v in got.items()},
                         {"usd_per_prompt": "up", "prompts_per_session": "down",
                          "compactions_per_session": "up", "reread_share": "up"})
        text = fixes.format_result(r)
        self.assertIn("$ per prompt: $2.00 -> $5.00 (up)", text)
        self.assertIn("Prompts per session: 5.00 -> 2.00 (down)", text)

    def test_all_windows_skipped_is_not_called_agreement(self):
        r = self._verify([True] * 12, [False] * 12)
        for w in r["baselines"]["windows"]:
            w["verdict"], w["reason"] = "skipped", "x"
        self.assertIn("no baseline window had enough", fixes.format_result(r))
        self.assertNotIn("baselines agree", fixes.format_result(r))

    def test_advisor_turns_do_not_double_a_compaction(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE turns (session_id, message_id, is_compact_summary, prompt_seq, cache_read_tokens, input_tokens, cache_creation_tokens)")
        conn.executemany("INSERT INTO turns VALUES ('s', ?, 1, 1, 0, 0, 0)", [("m1",), ("advisor:m1:0",)])
        self.assertEqual(fixes._counter_rows(conn)["s"][1], 1)

    def test_agent_summary_copies_do_not_double_a_compaction(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE turns (session_id, message_id, is_compact_summary, prompt_seq, cache_read_tokens, input_tokens, cache_creation_tokens)")
        conn.executemany("INSERT INTO turns VALUES ('s', ?, 1, 1, 0, 0, 0)", [("m1",), ("agent_summary:a1",)])
        self.assertEqual(fixes._counter_rows(conn)["s"][1], 1)

    def test_lost_result_sizes_never_read_as_a_saving(self):
        # same waste on both sides; then the after side's tool-result sizes are lost
        tmp, db = _build([True] * 12, [True] * 12)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fixes.record_fix(conn, "acme/app", "resident_output", "trim bash output", FIX_DATE)
        conn.execute("""UPDATE tool_results SET result_bytes = NULL WHERE session_id IN
                        (SELECT session_id FROM sessions WHERE date(first_timestamp, 'localtime') >= ?)""", (FIX_DATE,))
        self.assertGreater(conn.total_changes, 0)
        conn.commit()
        fix = fixes.list_fixes(conn)[0]
        conn.close()
        r = fixes.verify(db, fix)
        self.assertNotEqual(r["status"], "reduced")
        self.assertEqual(r["saving_usd"], 0.0)
        self.assertEqual(r["status"], "insufficient data")
        self.assertEqual((r["before"]["unmeasured"], r["after"]["unmeasured"]), (0, 12))
        self.assertIn("12 more could not be measured", r["reason"])
        # the Changes view goes through the same slice
        v = fixes.change_view(db, fix, today=date(2026, 9, 20))
        self.assertEqual(v["state"], "waiting")

    def test_selective_loss_withholds_the_verdict(self):
        # 12 wasteful + 12 clean on each side; only the after side's wasteful sessions lose their sizes.
        # The clean ones alone still pass the gate, so dropping the lost ones is not enough on its own.
        tmp, db = _build([True, False] * 12, [True, False] * 12)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fixes.record_fix(conn, "acme/app", "resident_output", "trim bash output", FIX_DATE)
        conn.execute("""UPDATE tool_results SET result_bytes = NULL WHERE session_id IN
                        (SELECT session_id FROM sessions WHERE date(first_timestamp, 'localtime') >= ?)""", (FIX_DATE,))
        conn.commit()
        fix = fixes.list_fixes(conn)[0]
        conn.close()
        r = fixes.verify(db, fix)
        self.assertEqual((r["after"]["sessions"], r["after"]["unmeasured"]), (12, 12))
        self.assertEqual((r["status"], r["saving_usd"]), ("insufficient data", 0.0))
        self.assertIn("12 sessions could not be measured", r["reason"])
        v = fixes.change_view(db, fix, today=date(2026, 9, 20))
        self.assertEqual((v["state"], v["interim"], v["eta_days"]), ("waiting", True, None))

    def test_unmeasured_comes_from_the_recorded_calls(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proj = Path(tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        sessions = {
            # its only tool call is the last turn: no later write to attribute, but its size is still unknown
            "terminal": _main_chain("terminal", 4, base=20_000, tool_at={4: [("Bash", {"command": "ls"})]}),
            # two calls, only one loses its error flag
            "partial": _main_chain("partial", 4, base=20_000,
                                   tool_at={2: [("Bash", {"command": "make"}), ("Read", {"file_path": "/a.py"})]}),
            "search": _main_chain("search", 4, base=20_000, tool_at={2: [("ToolSearch", {"query": "x"})]}),
        }
        for sid, lines in sessions.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        db = str(Path(tmp) / "usage.db")
        scan_all(db_path=db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(proj.parent)])])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE tool_results SET result_bytes = NULL WHERE session_id = 'terminal'")
        conn.execute("UPDATE tool_results SET is_error = NULL WHERE session_id = 'partial' AND tool_name = 'Bash'")
        conn.commit()
        conn.close()
        lost = {}
        fixes.waste.report(db, min_share=0, min_usd=0, unmeasured=lost)
        self.assertIn("resident_output", lost.get("terminal", set()))
        self.assertIn("retry_loop", lost.get("partial", set()))
        self.assertNotIn("resident_output", lost.get("partial", set()))
        # a ToolSearch whose loaded schemas were never recorded
        self.assertIn("deferred_tool_load", lost.get("search", set()))

    def test_lost_error_flags_leave_the_retry_cohort_only(self):
        tmp, db = _build([True] * 12, [True] * 12)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fixes.record_fix(conn, "acme/app", "retry_loop", "stop retrying", FIX_DATE)
        fixes.record_fix(conn, "acme/app", "resident_output", "trim bash output", FIX_DATE)
        conn.execute("""UPDATE tool_results SET is_error = NULL WHERE session_id IN
                        (SELECT session_id FROM sessions WHERE date(first_timestamp, 'localtime') >= ?)""", (FIX_DATE,))
        conn.commit()
        retry, resident = fixes.list_fixes(conn)
        conn.close()
        everything = fixes._gather_all(db)
        r = fixes.verify(db, retry, fixes._slice(everything, retry))
        self.assertEqual((r["before"]["unmeasured"], r["after"]["unmeasured"]), (0, 12))
        self.assertEqual(r["status"], "insufficient data")
        # result sizes are intact, so the resident-output cohort keeps every session
        r = fixes.verify(db, resident, fixes._slice(everything, resident))
        self.assertEqual((r["after"]["sessions"], r["after"]["unmeasured"]), (12, 0))

    def test_verdict_reads_one_database_state(self):
        # a scan commits between the spend reads and the waste report: the verdict must be rebuilt
        tmp, db = _build([True] * 12, [True] * 12)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = fixes.waste.report
        calls = []

        def report_after_a_commit(*a, **kw):
            if not calls:
                w = sqlite3.connect(db)
                w.execute("UPDATE turns SET cache_creation_tokens = cache_creation_tokens * 50")
                w.commit()
                w.close()
            calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(fixes.waste, "report", report_after_a_commit), mock.patch("time.sleep"):
            totals, _, cause_usd, _ = fixes._gather_all(db)[:4]
        self.assertEqual(len(calls), 2)
        conn = sqlite3.connect(db)
        self.assertEqual(totals, fixes._session_totals(conn))
        conn.close()
        for s, usd in cause_usd["resident_output"].items():
            self.assertLessEqual(usd, totals[s])

    def test_no_baselines_without_a_decided_verdict(self):
        self.assertNotIn("baselines", self._verify([True] * 12, [True] * 12))

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

    def test_exactly_one_point_is_material(self):
        # 3% to 2%: 0.03 - 0.02 is 0.00999... in floats and read as under the 1-point floor
        n = fixes.MIN_SESSIONS
        rows = [(f"b{i}", "2026-01-01", None) for i in range(n)] + [(f"a{i}", "2026-03-01", None) for i in range(n)]
        cause = {s: (3.0 if s.startswith("b") else 2.0) for s, _, _ in rows}
        fix = {"id": 1, "project": "p", "cause": "resident_output", "note": "", "fix_date": "2026-02-01"}
        r = fixes.verify(None, fix, ({s: 100.0 for s, _, _ in rows}, rows, cause))
        self.assertEqual((r["before"]["pct"], r["after"]["pct"], r["status"]), (0.03, 0.02, "reduced"))

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
