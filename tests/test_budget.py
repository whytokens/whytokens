"""Monthly budget: the file, month_status's arithmetic, the endpoints and the CLI.

scripts/falsify_budget.py breaks each piece and checks this file goes red.
"""

import io
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import date
from http.server import HTTPServer
from unittest import mock

import api
import budget
import cli
from tests.usage_fixture import make_db, november

TOKEN = "t0k"


class _Db(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = make_db(november())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def status(self, today, **kw):
        return budget.month_status(self.db, today=today, tz="utc", **kw)


class TestFile(_Db):
    def test_absent_is_none_never_zero(self):
        self.assertIsNone(budget.load_budget(self.db))
        self.assertIsNone(self.status(date(2026, 11, 10))["budget"])

    def test_round_trip_and_clear(self):
        self.assertEqual(budget.set_budget(250, self.db), 250.0)
        self.assertEqual(budget.load_budget(self.db), 250.0)
        self.assertEqual(json.loads(budget.budget_path(self.db).read_text()), {"monthly_usd": 250.0})
        self.assertEqual(budget.budget_path(self.db).parent, self.db.parent)
        budget.set_budget(None, self.db)
        self.assertIsNone(budget.load_budget(self.db))
        budget.set_budget(None, self.db)  # clearing twice is fine

    def test_invalid_values_rejected_and_file_untouched(self):
        budget.set_budget(10, self.db)
        for bad in (0, -5, float("nan"), float("inf"), True, "12", [], {}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                budget.set_budget(bad, self.db)
        self.assertEqual(budget.load_budget(self.db), 10.0)
        self.assertFalse(list(self.db.parent.glob("*.tmp")))

    def test_corrupt_file_reads_as_unset(self):
        budget.budget_path(self.db).write_text("{nope", encoding="utf-8")
        self.assertIsNone(budget.load_budget(self.db))
        budget.budget_path(self.db).write_text('{"monthly_usd": 0}', encoding="utf-8")
        self.assertIsNone(budget.load_budget(self.db))


class TestFileEdges(_Db):
    def test_a_number_too_big_for_a_float_is_invalid_not_a_crash(self):
        with self.assertRaises(ValueError):
            budget.set_budget(10 ** 400, self.db)
        budget.budget_path(self.db).write_text('{"monthly_usd": 1' + "0" * 400 + "}", encoding="utf-8")
        self.assertIsNone(budget.load_budget(self.db))

    def test_missing_data_directory_is_created(self):
        db = self.db.parent / "not" / "yet" / "usage.db"
        self.assertEqual(budget.set_budget(40, db), 40.0)
        self.assertEqual(budget.load_budget(db), 40.0)

    def test_two_writers_do_not_share_a_temp_file(self):
        real, state = budget.os.replace, {"inner": False}

        def interleaved(src, dst):
            if not state["inner"]:  # a second writer runs between the first one's write and its replace
                state["inner"] = True
                budget.set_budget(200, self.db)
            return real(src, dst)

        with mock.patch.object(budget.os, "replace", interleaved):
            budget.set_budget(100, self.db)
        self.assertEqual(budget.load_budget(self.db), 100.0)
        self.assertFalse(list(self.db.parent.glob("*.tmp")))


class TestMonthStatus(_Db):
    def test_trailing_week_projection_exact(self):
        budget.set_budget(150, self.db)
        m = self.status(date(2026, 11, 10))
        # days 1..10 spend $1..$10; the last 7 days (4..10) average $7 a day; 20 days remain
        self.assertEqual((m["spent"], m["daily_rate"], m["projected"]), (55.0, 7.0, 195.0))
        self.assertEqual((m["gap"], m["needed_daily"]), (45.0, 4.75))
        self.assertEqual((m["method"], m["month"], m["days_elapsed"], m["days_remaining"]),
                         ("trailing_7d", "2026-11", 10, 20))

    def test_under_budget_gap_is_negative(self):
        budget.set_budget(300, self.db)
        self.assertEqual(self.status(date(2026, 11, 10))["gap"], -105.0)

    def test_no_budget_leaves_gap_and_needed_rate_none(self):
        m = self.status(date(2026, 11, 10))
        self.assertEqual((m["budget"], m["gap"], m["needed_daily"]), (None, None, None))
        self.assertEqual(m["projected"], 195.0)

    def test_first_days_use_month_to_date(self):
        m = self.status(date(2026, 11, 3))
        self.assertEqual((m["method"], m["spent"], m["daily_rate"], m["projected"]), ("month_to_date", 6.0, 2.0, 60.0))
        self.assertEqual(self.status(date(2026, 11, 6))["method"], "month_to_date")
        m = self.status(date(2026, 11, 7))  # first day with a full week: days 1..7 = $28 / 7
        self.assertEqual((m["method"], m["daily_rate"]), ("trailing_7d", 4.0))

    def test_zero_spend_month(self):
        m = self.status(date(2026, 12, 15))
        self.assertEqual((m["spent"], m["projected"], m["daily_rate"]), (0.0, 0.0, 0.0))
        budget.set_budget(100, self.db)
        m = self.status(date(2026, 12, 15))
        self.assertEqual((m["gap"], m["needed_daily"]), (-100.0, round(100 / 16, 6)))

    def test_missing_database_is_a_zero_month(self):
        m = budget.month_status(self.db.with_name("none.db"), today=date(2026, 11, 10), tz="utc")
        self.assertEqual((m["spent"], m["projected"], m["budget"]), (0.0, 0.0, None))

    def test_last_day_has_no_needed_rate(self):
        budget.set_budget(150, self.db)
        m = self.status(date(2026, 11, 30))
        self.assertEqual((m["days_remaining"], m["needed_daily"], m["projected"]), (0, None, 55.0))

    def test_calendar_month_only(self):
        db, tmp = make_db(november() + [{"day": "2026-10-31", "output": 5_000_000},
                                        {"day": "2026-12-01", "output": 5_000_000}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        m = budget.month_status(db, today=date(2026, 11, 10), tz="utc")
        self.assertEqual((m["spent"], m["projected"]), (55.0, 195.0))

    def test_data_after_today_is_not_counted(self):
        self.assertEqual(self.status(date(2026, 11, 5))["spent"], 15.0)

    def test_spend_follows_the_dashboards_costing(self):
        # fast mode doubles a turn, exactly as api.get_daily prices it
        db, tmp = make_db([{"day": "2026-11-01", "output": 50_000, "speed": "fast"}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        want = api.get_daily(start="2026-11-01", end="2026-11-01", db_path=db)["totals"]["cost"]
        self.assertEqual(want, 2.0)
        self.assertEqual(budget.month_status(db, today=date(2026, 11, 1), tz="utc")["spent"], want)


class _Server(_Db):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(api, "DB_PATH", self.db), mock.patch.object(api.DashboardHandler, "token", TOKEN)):
            p.start(); self.addCleanup(p.stop)
        self.server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/budget"

    def post(self, body, token=TOKEN, raw=None):
        req = urllib.request.Request(self.base, data=raw if raw is not None else json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Whytokens-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestEndpoint(_Server):
    def test_get_reports_the_month(self):
        with urllib.request.urlopen(self.base + "?tz=utc") as r:
            got = json.loads(r.read())
        self.assertEqual(set(got), set(budget.month_status(self.db, today=date(2026, 11, 10), tz="utc")))
        self.assertIsNone(got["budget"])

    def test_post_sets_and_clears(self):
        status, got = self.post({"monthly_usd": 321.5})
        self.assertEqual((status, got["budget"]), (200, 321.5))
        self.assertEqual(budget.load_budget(self.db), 321.5)
        status, got = self.post({"monthly_usd": None})
        self.assertEqual((status, got["budget"]), (200, None))
        self.assertIsNone(budget.load_budget(self.db))

    def test_invalid_values_are_400(self):
        for body in ({"monthly_usd": 0}, {"monthly_usd": -1}, {"monthly_usd": "5"}, {"monthly_usd": True}, {}, [], {"x": 1}):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.post(None, raw=b"{bad")[0], 400)
        self.assertIsNone(budget.load_budget(self.db))

    def test_post_needs_the_token(self):
        self.assertEqual(self.post({"monthly_usd": 5}, token="wrong")[0], 403)
        self.assertEqual(self.post({"monthly_usd": 5}, token="")[0], 403)
        self.assertIsNone(budget.load_budget(self.db))


class TestCli(_Db):
    def run_cli(self, *args, today=date(2026, 11, 10)):
        buf = io.StringIO()
        with mock.patch.object(cli, "DB_PATH", self.db), redirect_stdout(buf):
            cli.cmd_budget(list(args), today=today)
        return buf.getvalue()

    def test_prints_the_five_numbers_and_the_method(self):
        self.run_cli("150")
        out = self.run_cli()
        for want in ("$55.00", "$150.00", "$195.00", "over by $45.00", "$4.75/day", "trailing 7-day"):
            self.assertIn(want, out)

    def test_no_budget_prints_spend_projection_and_a_hint(self):
        out = self.run_cli()
        self.assertIn("$55.00", out); self.assertIn("$195.00", out); self.assertIn("not set", out)
        self.assertNotIn("$0.00", out)

    def test_month_to_date_method_line(self):
        self.assertIn("month-to-date", self.run_cli(today=date(2026, 11, 3)))

    def test_set_and_clear(self):
        self.run_cli("99")
        self.assertEqual(budget.load_budget(self.db), 99.0)
        self.assertIn("cleared", self.run_cli("--clear"))
        self.assertIsNone(budget.load_budget(self.db))

    def test_bad_amount_exits_2(self):
        for bad in ("0", "-3", "abc", "nan"):
            with self.assertRaises(SystemExit) as e:
                self.run_cli(bad)
            self.assertEqual(e.exception.code, 2, bad)
        self.assertIsNone(budget.load_budget(self.db))


if __name__ == "__main__":
    unittest.main()
