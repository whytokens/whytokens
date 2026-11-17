"""Model what-if: repricing is observation only and keeps every price multiplier.

scripts/falsify_budget.py breaks each piece and checks this file goes red.
"""

import json
from pathlib import Path
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest import mock

import api
import pricing
import waste
import whatif
from tests.usage_fixture import OPUS, make_db

SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
DAY = "2026-11-02"

# One turn per case; each exercises a multiplier or token class the identity must survive.
CASES = {
    "plain": {"input": 1000, "output": 2000, "cache_read": 30000},
    "fast": {"output": 50_000, "speed": "fast"},
    "us_geo": {"input": 7000, "output": 9000, "geo": "us"},
    "fast_and_us": {"output": 50_000, "speed": "fast", "geo": "us"},
    "cache_write_5m": {"cache_creation": 40_000, "output": 10},
    "cache_write_1h": {"cache_creation": 40_000, "cc1h": 40_000, "output": 10},
    "cache_write_mixed": {"cache_creation": 40_000, "cc1h": 15_000, "input": 5, "output": 10},
    "cache_read_heavy": {"cache_read": 900_000, "input": 10, "output": 10},
}


def recorded(db, model=""):
    """What the dashboard's own cost path (api.get_daily) says these turns cost."""
    return api.get_daily(model=model, db_path=db)["totals"]["cost"]


class TestIdentity(unittest.TestCase):
    def check(self, turn, model):
        db, tmp = make_db([{"day": DAY, "model": model, **turn}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        got = whatif.reprice(model, db_path=db)
        self.assertAlmostEqual(got["repriced_usd"], recorded(db), places=2, msg=turn)
        self.assertAlmostEqual(got["actual_usd"], recorded(db), places=2, msg=turn)
        self.assertAlmostEqual(got["delta_usd"], 0.0, places=2)
        self.assertGreater(got["actual_usd"], 0)

    def test_own_model_equals_recorded_cost(self):
        for name, turn in CASES.items():
            with self.subTest(name):
                self.check(turn, OPUS)

    def test_identity_on_a_tiered_and_an_older_model(self):
        for model in (SONNET, "claude-sonnet-4-6", "claude-fable-5-2"):
            for name in ("fast_and_us", "cache_write_mixed"):
                with self.subTest(model=model, case=name):
                    self.check(CASES[name], model)

    def test_long_prompt_tier(self):
        # Haiku 5.5 bills 5x on every class once input + cache_read + cache_creation > 100K
        self.check({"input": 60_000, "cache_read": 60_000, "output": 5000}, HAIKU)
        self.check({"input": 60_000, "cache_read": 30_000, "output": 5000}, HAIKU)
        db, tmp = make_db([{"day": DAY, "model": HAIKU, "input": 60_000, "cache_read": 60_000, "output": 5000}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        short, tmp2 = make_db([{"day": DAY, "model": HAIKU, "input": 60_000, "cache_read": 30_000, "output": 5000}])
        self.addCleanup(shutil.rmtree, tmp2, ignore_errors=True)
        self.assertAlmostEqual(whatif.reprice(HAIKU, db_path=db)["actual_usd"], 5 * 0.0091, places=6)
        self.assertAlmostEqual(whatif.reprice(HAIKU, db_path=short)["actual_usd"], 0.0088, places=6)
        # repricing a small-prompt Sonnet turn onto Haiku triggers nothing; a 120K one does
        big, tmp3 = make_db([{"day": DAY, "model": SONNET, "input": 60_000, "cache_read": 60_000, "output": 5000}])
        self.addCleanup(shutil.rmtree, tmp3, ignore_errors=True)
        self.assertAlmostEqual(whatif.reprice(HAIKU, db_path=big)["repriced_usd"], 5 * 0.0091, places=6)

    def test_cache_write_rate_matters(self):
        # 40K cache writes on Opus 5.5: $5/MTok 5m, $8/MTok 1h; the target's own rates apply
        db, tmp = make_db([{"day": DAY, "model": OPUS, "cache_creation": 40_000, "cc1h": 40_000}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertAlmostEqual(whatif.reprice(OPUS, db_path=db)["repriced_usd"], 0.32, places=6)
        self.assertAlmostEqual(whatif.reprice(SONNET, db_path=db)["repriced_usd"], 0.16, places=6)


class TestRepricing(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = make_db(
            [{"day": DAY, "model": OPUS, "output": 100_000, "speed": "fast", "geo": "us", "cache_creation": 10_000},
             {"day": "2026-11-03", "model": OPUS, "output": 100_000, "session": "s2"},
             {"day": DAY, "model": SONNET, "output": 100_000, "session": "s2"}])
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_cheaper_target_lowers_cost_and_keeps_multipliers(self):
        one, tmp = make_db([{"day": DAY, "model": OPUS, "output": 100_000, "speed": "fast", "geo": "us"}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        got = whatif.reprice(SONNET, db_path=one)
        self.assertAlmostEqual(got["actual_usd"], 100_000 * 20 / 1e6 * 2.2, places=6)
        self.assertAlmostEqual(got["repriced_usd"], 100_000 * 10 / 1e6 * 2.2, places=6)  # 2x fast x 1.1 us survive
        self.assertLess(got["delta_usd"], 0)
        self.assertEqual(got["wording"], "Repricing only. It does not say the cheaper model would have done the work.")

    def test_totals_are_the_sum_of_the_by_model_split(self):
        got = whatif.reprice(HAIKU, db_path=self.db)
        self.assertEqual({m["model"] for m in got["by_model"]}, {OPUS, SONNET})
        self.assertAlmostEqual(sum(m["repriced_usd"] for m in got["by_model"]), got["repriced_usd"], places=5)
        self.assertAlmostEqual(sum(m["actual_usd"] for m in got["by_model"]), got["actual_usd"], places=5)

    def test_scope_params(self):
        self.assertEqual(whatif.reprice(SONNET, start="2026-11-03", db_path=self.db)["by_model"][0]["turns"], 1)
        self.assertEqual(sum(m["turns"] for m in whatif.reprice(SONNET, end="2026-11-02", db_path=self.db)["by_model"]), 2)
        self.assertEqual([m["model"] for m in whatif.reprice(HAIKU, model=SONNET, db_path=self.db)["by_model"]], [SONNET])
        self.assertEqual(sum(m["turns"] for m in whatif.reprice(HAIKU, project="acme", db_path=self.db)["by_model"]), 3)
        self.assertEqual(whatif.reprice(HAIKU, project="nope", db_path=self.db)["by_model"], [])
        self.assertEqual(sum(m["turns"] for m in whatif.reprice(HAIKU, session_id="s2", db_path=self.db)["by_model"]), 2)

    def test_next_tier_mode_reprices_each_model_on_its_own_tier(self):
        got = whatif.reprice(whatif.NEXT_TIER, db_path=self.db)
        by = {m["model"]: m for m in got["by_model"]}
        self.assertEqual((by[OPUS]["to"], by[SONNET]["to"]), (SONNET, HAIKU))
        self.assertLess(by[OPUS]["delta_usd"], 0)

    def test_model_without_a_cheaper_tier_has_no_figure(self):
        db, tmp = make_db([{"day": DAY, "model": HAIKU, "output": 1000}, {"day": DAY, "model": "gpt-5.4", "output": 1000}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        got = whatif.reprice(whatif.NEXT_TIER, db_path=db)
        self.assertEqual([(m["to"], m["delta_usd"]) for m in got["by_model"]], [(None, None), (None, None)])
        self.assertEqual((got["actual_usd"], got["repriced_usd"], got["delta_usd"]), (0, 0, 0))

    def test_unknown_target_is_an_error(self):
        for to in ("", "gpt-9-imaginary", "nonsense"):
            self.assertIn("error", whatif.reprice(to, db_path=self.db), to)


class TestActorFilter(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = make_db(
            [{"day": DAY, "output": 100_000},
             {"day": DAY, "output": 50_000, "agent_id": "a1"},
             {"day": DAY, "output": 50_000, "agent_id": "a2"},
             {"day": DAY, "output": 25_000, "agent_id": "a3"}],
            agents=[("a1", "Explore"), ("a2", "Explore"), ("a3", "Plan")])
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def actual(self, actor):
        return whatif.reprice(SONNET, actor=actor, db_path=self.db)["actual_usd"]

    def test_lanes(self):
        self.assertAlmostEqual(self.actual("main"), 2.0)
        self.assertAlmostEqual(self.actual("subagent"), 2.5)
        self.assertAlmostEqual(self.actual("Explore"), 2.0)
        self.assertAlmostEqual(self.actual("Plan"), 0.5)
        self.assertAlmostEqual(self.actual(""), 4.5)
        self.assertEqual(whatif.reprice(SONNET, actor="Nobody", db_path=self.db)["by_model"], [])

    def test_lanes_match_the_session_breakdown(self):
        rows = api.get_session_breakdown("s1", self.db)["rows"]
        self.assertEqual(sum(r["output"] for r in rows if r["kind"] == "agent"), 125_000)
        self.assertEqual(self.actual("subagent"), round(125_000 * 20 / 1e6, 6))


class TestUntypedLane(unittest.TestCase):
    def test_a_sidechain_turn_without_an_agent_row_is_a_subagent_not_main(self):
        db, tmp = make_db([{"day": DAY, "output": 100_000},
                           {"day": DAY, "output": 50_000, "agent_id": "ghost", "sidechain": 1}])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        got = lambda actor: whatif.reprice(SONNET, actor=actor, db_path=db)["actual_usd"]
        self.assertAlmostEqual(got("main"), 2.0)
        self.assertAlmostEqual(got("subagent"), 1.0)


class TestDayBuckets(unittest.TestCase):
    def test_local_day_windows_match_the_dashboards_daily_buckets(self):
        db, tmp = make_db([{"day": d, "output": 10_000 * (i + 1)} for i, d in enumerate(("2026-11-01", "2026-11-02", "2026-11-03"))])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for row in api.get_daily(tz="local", db_path=db)["daily"]:
            got = whatif.reprice(OPUS, start=row["day"], end=row["day"], tz="local", db_path=db)["actual_usd"]
            self.assertAlmostEqual(got, row["cost"], places=6, msg=row["day"])


class TestLocalDayIsReallyLocal(unittest.TestCase):
    def test_a_turn_after_local_midnight_belongs_to_the_next_local_day(self):
        import os
        import subprocess
        import sys
        db, tmp = make_db([{"day": "2026-11-02", "output": 10_000}])  # 12:00Z, which is 02:00 on the 3rd at UTC+14
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        code = ("import json, whatif; from pathlib import Path; db = Path(%r); "
                "f = lambda d, tz: whatif.reprice('claude-opus-5-5', start=d, end=d, tz=tz, db_path=db)['actual_usd']; "
                "print(json.dumps([f('2026-11-03', 'local'), f('2026-11-02', 'local'), f('2026-11-02', 'utc')]))" % str(db))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
                             env={**os.environ, "TZ": "UTC-14"}, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        next_day, same_day, utc_day = json.loads(out.stdout)
        self.assertGreater(next_day, 0)
        self.assertEqual(same_day, 0)
        self.assertGreater(utc_day, 0)


class TestTiers(unittest.TestCase):
    def test_order_is_strictly_cheaper_and_priced(self):
        rates = [pricing.get_pricing(m)["input"] for m in pricing.TIER_ORDER]
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertEqual(len(set(rates)), len(rates))

    def test_next_tier_down(self):
        n = pricing.next_tier_down
        self.assertEqual(n("claude-fable-5-2"), "claude-opus-5-5")
        self.assertEqual(n("claude-mythos-5-1"), "claude-opus-5-5")
        self.assertEqual(n("claude-opus-4-8"), SONNET)
        self.assertEqual(n("claude-sonnet-4-5-20250929"), HAIKU)
        self.assertIsNone(n(HAIKU))
        for other in ("gpt-5.4", "unknown", "", None):
            self.assertIsNone(n(other), other)
        for m in pricing.TIER_ORDER[:-1]:  # always concrete, priced and cheaper
            nxt = n(m)
            self.assertLess(pricing.get_pricing(nxt)["input"], pricing.get_pricing(m)["input"])


class TestEndpointAndIsolation(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = make_db([{"day": DAY, "model": OPUS, "output": 100_000, "session": "s1"}])
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(api, "DB_PATH", self.db)
        p.start(); self.addCleanup(p.stop)
        self.server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/whatif"

    def get(self, qs):
        try:
            with urllib.request.urlopen(self.base + "?" + qs) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_http_route(self):
        status, got = self.get(f"to={SONNET}&start={DAY}&end={DAY}&model={OPUS}&project=acme&actor=main")
        self.assertEqual(status, 200)
        self.assertEqual((got["to"], got["actual_usd"], got["repriced_usd"], got["delta_usd"]), (SONNET, 2.0, 1.0, -1.0))
        self.assertEqual(self.get(f"to={SONNET}&actor=subagent")[1]["by_model"], [])
        self.assertEqual(self.get(f"to={whatif.NEXT_TIER}")[1]["by_model"][0]["to"], SONNET)

    def test_unknown_target_is_400_json(self):
        status, got = self.get("to=nope")
        self.assertEqual(status, 400)
        self.assertIn("error", got)
        self.assertEqual(self.get("")[0], 400)

    def test_what_if_never_leaks_into_other_totals(self):
        before = (json.dumps(waste.report(self.db), sort_keys=True, default=str),
                  json.dumps(api.get_waste(self.db), sort_keys=True, default=str),
                  json.dumps(api.get_daily(db_path=self.db), sort_keys=True),
                  json.dumps(api.get_sessions_page(db_path=self.db), sort_keys=True, default=str))
        whatif.reprice(whatif.NEXT_TIER, db_path=self.db)
        self.get(f"to={HAIKU}")
        after = (json.dumps(waste.report(self.db), sort_keys=True, default=str),
                 json.dumps(api.get_waste(self.db), sort_keys=True, default=str),
                 json.dumps(api.get_daily(db_path=self.db), sort_keys=True),
                 json.dumps(api.get_sessions_page(db_path=self.db), sort_keys=True, default=str))
        self.assertEqual(before, after)
        # and the totals are what their own rows say, whatever a what-if would add
        waste_payload = api.get_waste(self.db)
        # observation causes (in_total false) are shown but never part of the total
        self.assertEqual(waste_payload["total_usd"], sum(f["usd"] for f in waste_payload["findings"] if f["in_total"]))
        self.assertAlmostEqual(sum(r["usd"] for r in waste_payload["rollup"] if r["in_total"]), waste_payload["total_usd"])
        daily = api.get_daily(db_path=self.db)
        self.assertAlmostEqual(daily["totals"]["cost"], sum(d["cost"] for d in daily["daily"]), places=5)
        self.assertEqual(daily["totals"]["cost"], 2.0)  # the Opus turn at its own price, not 1.0 repriced
        for payload in before:
            self.assertNotIn("repriced", payload)
            self.assertNotIn("whatif", payload)

    def test_waste_module_does_not_know_what_if(self):
        import inspect
        self.assertNotIn("whatif", inspect.getsource(waste))
        self.assertNotIn("reprice", inspect.getsource(waste))


if __name__ == "__main__":
    unittest.main()
