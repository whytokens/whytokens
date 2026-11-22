"""Reconcile against the bill: each named cause row equals the amount planted for it, and each
mutant moves only its own row. scripts/falsify_reconcile.py breaks each row's query and checks
this file goes red.

The base fixture reproduces the November shape: shown $300.83, billed $285.63, estimated
summaries $4.06 ($4.50 with their surcharge), Haiku 5.5 5x premium $4.31, remainder $6.39.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import cli
import reconcile
from scanner import get_db, init_db

OPUS, HAIKU, CODEX = "claude-opus-5-5", "claude-haiku-5-5", "gpt-6.1-sol"
DAY = "2026-11-10T12:00:00Z"   # noon UTC: the same local day in every time zone within +-12h
AFTER = date(2026, 12, 5)


def opus(usd, **kw):        # $20 / MTok output
    return dict(model=OPUS, output=round(usd * 50_000), **kw)


def base():
    return [
        dict(ts="2026-10-15T12:00:00Z", model=OPUS, session="old"),            # data floor, $0
        opus(289.9425),                                                          # ordinary spend
        opus(1.00, mid="advisor:m9:est", estimated=1),                           # info only
        opus(3.95, mid="agent_summary:a1", estimated=1, sidechain=1),            # summary, plain
        dict(model=HAIKU, input=1_100_000, mid="agent_summary:a2", estimated=1),  # summary, $0.11 x5
        dict(model=HAIKU, input=10_775_000),                                     # $1.0775 x5 = $5.3875
    ]


def make(turns):
    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "usage.db"
    conn = get_db(db)
    init_db(conn)
    for i, t in enumerate(turns):
        sid = t.get("session", "s1")
        conn.execute("INSERT OR IGNORE INTO sessions (session_id, project_name) VALUES (?, ?)",
                     (sid, t.get("project", "acme")))
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens, message_id,"
            " estimated, is_sidechain, agent) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, t.get("ts", DAY), t["model"], t.get("input", 0), t.get("output", 0),
             t.get("mid", f"m{i}"), t.get("estimated", 0), t.get("sidechain", 0), t.get("agent", "claude")))
    conn.commit()
    conn.close()
    return db, tmp


class _Case(unittest.TestCase):
    def run_case(self, turns, billed=285.63):
        db, tmp = make(turns)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        if billed is not None:
            reconcile.set_billed("2026-11", billed, db_path=db)
        self.db = db
        r = reconcile.report("2026-11", db, today=AFTER)
        r["by"] = {row["key"]: row for row in r["rows"]}
        return r

    def assertRows(self, r, **want):
        for k, v in want.items():
            self.assertAlmostEqual(r["by"][k]["usd"], v, places=6, msg=k)


class TestNovemberShape(_Case):
    def test_each_row_equals_its_plant_and_all_sum_to_the_gap(self):
        r = self.run_case(base())
        self.assertAlmostEqual(r["shown"], 300.83, places=6)
        self.assertAlmostEqual(r["gap"], 15.20, places=6)
        self.assertRows(r, day_boundary=0, other_vendor=0, estimated=4.50, haiku_5x=4.31)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)
        self.assertAlmostEqual(sum(x["usd"] for x in r["rows"]) + r["remainder"]["usd"], r["gap"], places=6)
        self.assertEqual(r["by"]["haiku_5x"]["turns"], 1)       # the summary Haiku row is row a's, not b's
        self.assertIn("$5.39 with, $1.08 without", r["by"]["haiku_5x"]["label"])
        self.assertEqual(r["coverage"]["days_before_earliest"], 0)
        self.assertEqual([x["kind"] for x in r["rows"]], ["measured", "measured", "scenario", "scenario"])
        self.assertAlmostEqual(r["info"]["advisor_estimates"]["usd"], 1.00, places=6)

    def test_no_billed_leaves_gap_and_remainder_unknown(self):
        r = self.run_case(base(), billed=None)
        self.assertIsNone(r["gap"])
        self.assertIsNone(r["remainder"]["usd"])
        self.assertRows(r, estimated=4.50, haiku_5x=4.31)


class TestMutants(_Case):
    """Each mutant shifts one cause; that row moves and the other named rows hold."""

    def test_time_zone(self):
        off = datetime(2026, 11, 30, tzinfo=timezone.utc).astimezone().utcoffset().total_seconds()
        if not off:
            self.skipTest("local time is UTC here: local and UTC cuts coincide")
        # 30 minutes past the UTC cut on the side where the local day is still inside November
        ts = "2026-12-01T00:30:00Z" if off < 0 else "2026-10-31T23:30:00Z"
        r = self.run_case(base() + [opus(1.00, ts=ts)])
        self.assertRows(r, day_boundary=1.00, other_vendor=0, estimated=4.50, haiku_5x=4.31)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)

    def test_deleted_transcripts(self):
        r = self.run_case(base()[1:])                    # no history before the 10th
        c = r["coverage"]
        self.assertEqual(c["days_before_earliest"], 9)
        self.assertIn("first 9 day(s)", c["label"])
        self.assertRows(r, day_boundary=0, estimated=4.50, haiku_5x=4.31)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)  # not priced: it stays unexplained
        self.assertEqual([p["first_seen"] for p in c["projects_first_seen_in_period"]], ["2026-11-10"])

    def test_estimated_rows_counted_as_real(self):
        turns = base()
        for t in turns:
            if t.get("mid", "").startswith("agent_summary:"):
                t["mid"], t["estimated"] = "real" + t["mid"][13:], 0
        r = self.run_case(turns)
        # the Haiku summary's surcharge moves to row b; the rest lands in the remainder
        self.assertRows(r, estimated=0, haiku_5x=4.31 + 0.44)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39 + 4.06, places=6)

    def test_haiku_under_the_line(self):
        turns = base()
        turns[-1]["input"] = 100_000                       # at the line, not over it: no 5x
        r = self.run_case(turns)
        self.assertRows(r, haiku_5x=0, estimated=4.50)
        self.assertAlmostEqual(r["shown"], 300.83 - 5.3875 + 0.01, places=6)

    def test_codex_outside_the_local_month_moves_two_measured_rows(self):
        off = datetime(2026, 11, 30, tzinfo=timezone.utc).astimezone().utcoffset().total_seconds()
        if not off:
            self.skipTest("local time is UTC here")
        # inside the UTC month, outside the local one: boundary -$2, vendor +$2, nothing else moves
        ts = "2026-11-30T23:30:00Z" if off > 0 else "2026-11-01T00:30:00Z"
        r = self.run_case(base() + [dict(model=CODEX, output=200_000, agent="codex", ts=ts)])
        self.assertRows(r, day_boundary=-2.00, other_vendor=2.00, estimated=4.50, haiku_5x=4.31)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)

    def test_haiku_premium_skips_aggregated_rows(self):
        r = self.run_case(base() + [dict(model=HAIKU, input=200_000, agent="coordinator"),
                                    dict(model=HAIKU, input=200_000, mid="advisor:m8:est", estimated=1)])
        self.assertRows(r, haiku_5x=4.31, estimated=4.50)

    def test_mid_month_bill_cuts_whytokens_at_its_reading(self):
        db, tmp = make(base() + [opus(7.00, ts="2026-11-10T18:00:00Z")])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reconcile.set_billed("2026-11", 285.63, "2026-11-10T15:00:00+00:00", db_path=db)
        r = reconcile.report("2026-11", db, today=AFTER)
        self.assertEqual(r["as_of"], "2026-11-10T15:00:00.000000Z")
        self.assertAlmostEqual(r["shown"], 300.83, places=6)          # the 18:00 turn is after the reading
        self.assertAlmostEqual(r["utc_total"], 300.83, places=6)

    def test_cutoff_keeps_fractional_seconds(self):
        db, tmp = make(base() + [opus(10.00, ts="2026-11-10T12:00:00.250Z")])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reconcile.set_billed("2026-11", 285.63, "2026-11-10T12:00:00.500Z", db_path=db)
        r = reconcile.report("2026-11", db, today=AFTER)
        self.assertAlmostEqual(r["utc_total"], 310.83, places=6)       # 12:00:00.250 is before the reading
        self.assertAlmostEqual(r["shown"], 310.83, places=6)

    def test_cutoff_compares_exact_instants_below_a_millisecond(self):
        db, tmp = make(base() + [opus(10.00, ts="2026-11-10T12:00:00.100Z")])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reconcile.set_billed("2026-11", 285.63, "2026-11-10T12:00:00.100400Z", db_path=db)
        r = reconcile.report("2026-11", db, today=AFTER)
        self.assertAlmostEqual(r["utc_total"], 310.83, places=6)       # 400 us before the reading: counted
        self.assertAlmostEqual(r["shown"], 310.83, places=6)

    def test_space_separated_and_overflowing_stored_timestamps(self):
        db, tmp = make(base() + [opus(10.00, ts="2026-11-10 12:00:00"), opus(5.00, ts="9999-12-31T23:59:59-05:00")])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = reconcile.report("2026-11", db, today=AFTER)
        self.assertAlmostEqual(r["utc_total"], 310.83, places=6)

    def test_cutoff_after_the_utc_month_still_cuts_the_local_month(self):
        db, tmp = make(base() + [opus(20.00, ts="2026-12-01T02:00:00Z")])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        reconcile.set_billed("2026-11", 285.63, "2026-12-01T01:00:00Z", db_path=db)
        self.assertAlmostEqual(reconcile.report("2026-11", db, today=AFTER)["shown"], 300.83, places=6)

    def test_request_rows_without_a_message_id_still_count(self):
        turns = base()
        turns[-1]["mid"] = None
        self.assertRows(self.run_case(turns), haiku_5x=4.31, estimated=4.50)

    def test_codex_is_its_own_row(self):
        r = self.run_case(base() + [dict(model=CODEX, output=200_000, agent="codex")])
        self.assertRows(r, other_vendor=2.00, estimated=4.50, haiku_5x=4.31)
        self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)


class TestStorageAndCli(_Case):
    def test_billed_round_trip_clear_and_validation(self):
        self.run_case(base(), billed=None)
        reconcile.set_billed("2026-11", 285.63, db_path=self.db)
        self.assertEqual(reconcile.load_billed("2026-11", self.db), {"usd": 285.63, "as_of": None})
        reconcile.set_billed("2026-11", 0, db_path=self.db)               # $0.00 is a real reading
        self.assertEqual(reconcile.load_billed("2026-11", self.db)["usd"], 0.0)
        for bad in (-1, float("nan"), float("inf"), True, "12"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                reconcile.set_billed("2026-11", bad, db_path=self.db)
        for bad in ("2026-13", "26-11", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                reconcile.set_billed(bad, 1, db_path=self.db)
        for bad in ("yesterday", "2026-11-10", 5, False):
            with self.assertRaises(ValueError, msg=repr(bad)):
                reconcile.set_billed("2026-11", 1, bad, db_path=self.db)
        for bad in ("2026-11\n", "２０２６-11", "2099-01"):                 # newline, non-ASCII digits, future
            with self.assertRaises(ValueError, msg=repr(bad)):
                reconcile.set_billed(bad, 1, db_path=self.db)
        self.assertEqual(set(json.loads(__import__("budget").budget_path(self.db).read_text())["billed"]), {"2026-11"})
        reconcile.set_billed("2026-11", None, db_path=self.db)
        self.assertIsNone(reconcile.load_billed("2026-11", self.db))
        with self.assertRaises(ValueError):
            reconcile.report("2027-01", self.db, today=AFTER)

    def test_budget_survives_and_is_kept(self):
        import budget
        self.run_case(base(), billed=None)
        budget.set_budget(500, self.db)
        reconcile.set_billed("2026-11", 285.63, db_path=self.db)
        self.assertEqual(budget.load_budget(self.db), 500.0)
        self.assertEqual(json.loads(budget.budget_path(self.db).read_text())["billed"],
                         {"2026-11": {"usd": 285.63, "as_of": None}})

    def test_unreadable_settings_file_is_never_overwritten(self):
        import budget
        self.run_case(base(), billed=None)
        path = budget.budget_path(self.db)
        path.write_text('{"monthly_usd": 500, ', encoding="utf-8")
        for save in (lambda: reconcile.set_billed("2026-11", 1, db_path=self.db),
                     lambda: budget.set_budget(10, self.db)):
            with self.assertRaises(ValueError):
                save()
        self.assertEqual(path.read_text(encoding="utf-8"), '{"monthly_usd": 500, ')

    def test_session_check(self):
        self.run_case(base())
        s = reconcile.session_check("s1", 290.00, self.db)
        self.assertAlmostEqual(s["whytokens"], 300.83, places=6)
        self.assertAlmostEqual(s["delta"], 10.83, places=6)
        self.assertAlmostEqual(s["estimated"], 4.50, places=6)
        self.assertAlmostEqual(s["haiku_5x"], 4.31, places=6)
        self.assertEqual(s["last_turn"], DAY)
        with self.assertRaises(ValueError):
            reconcile.session_check("nope", None, self.db)

    def test_cli_table_and_json(self):
        self.run_case(base(), billed=None)
        with mock.patch.object(cli, "DB_PATH", self.db):
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_reconcile(["--period", "2026-11", "--billed", "285.63"], today=AFTER)
            self.assertIn("$15.20", out.getvalue())
            self.assertIn("$6.39", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_reconcile(["--period", "2026-11", "--json"], today=AFTER)
            self.assertAlmostEqual(json.loads(out.getvalue())["remainder"]["usd"], 6.39, places=6)
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cli.cmd_reconcile(["--period", "2026-11", "--billed", "abc"], today=AFTER)


class TestApiAndPanel(_Case):
    def test_endpoints(self):
        import api
        self.run_case(base(), billed=None)
        with mock.patch.object(api, "DB_PATH", self.db),                 mock.patch.object(reconcile, "current_period", return_value="2026-11"):
            r = api.post_reconcile({"period": "2026-11", "billed": 285.63})
            self.assertTrue(r["as_of"])                                    # read mid-month: cut at the reading
        with mock.patch.object(api, "DB_PATH", self.db),                 mock.patch.object(reconcile, "current_period", return_value="2026-12"):
            r = api.post_reconcile({"period": "2026-11", "billed": 285.63})
            self.assertAlmostEqual(r["remainder"]["usd"], 6.39, places=6)
            self.assertIsNone(r["as_of"])                                  # a closed month has no cutoff
            self.assertAlmostEqual(api.get_reconcile({"period": ["2026-11"]})["gap"], 15.20, places=6)
            self.assertEqual(api.get_reconcile({"period": ["2026-11"], "tz": ["utc"]})["tz"], "utc")
            self.assertIn("error", api.get_reconcile({"period": ["2026-11"], "tz": ["mars"]}))
            self.assertIn("error", api.get_reconcile({"period": ["2026-13"]}))
            s = api.get_reconcile({"session": ["s1"], "usage": ["290"]})
            self.assertAlmostEqual(s["delta"], 10.83, places=6)
            with self.assertRaises(ValueError):
                api.post_reconcile({"billed": 1})
            api.post_reconcile({"period": "2026-11", "billed": None})
            self.assertIsNone(reconcile.load_billed("2026-11", self.db))

    def test_panel_is_wired(self):
        root = Path(__file__).resolve().parent.parent / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        for el in ("reconcile", "rec-period", "rec-billed", "rec-asof", "rec-save", "rec-clear", "rec-out",
                   "rec-session", "rec-usage", "rec-check", "rec-session-out"):
            self.assertIn(f'id="{el}"', html)
        app = (root / "modules" / "app.js").read_text(encoding="utf-8")
        self.assertIn("refreshReconcile = initReconcile(() => displayTZ);", app)
        self.assertIn("  displayTZ = mode;\n  refreshReconcile?.();", app)


if __name__ == "__main__":
    unittest.main()
