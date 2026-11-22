"""Claude Code's own `cost-state` record, stored per session and compared at read time."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import api
import cost_state
import pricing
import reconcile
import scanner
from scanner import get_db
from sources import ClaudeSource, scan_all

SID = "cs-session-1"
MODEL = "claude-opus-5-5"
T0 = "2026-11-10T10:00:00Z"
T0_MS = 1794304800000  # T0 in epoch ms
DAY = T0[:10]


def _prompt(ts=T0):
    return json.dumps({"type": "user", "sessionId": SID, "timestamp": ts, "cwd": "/tmp/p",
                       "message": {"role": "user", "content": "hi"}})


def _asst(mid, out, ts="2026-11-10T10:00:01Z", model=MODEL, cache_read=0):
    return json.dumps({"type": "assistant", "sessionId": SID, "timestamp": ts, "cwd": "/tmp/p",
                       "message": {"id": mid, "model": model, "role": "assistant",
                                   "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                   "usage": {"input_tokens": 0, "output_tokens": out,
                                             "cache_read_input_tokens": cache_read}}})


def _usage(out=0, cost=None, model=MODEL, cache_read=0, inp=0, **extra):
    u = {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": cache_read,
         "cacheCreationInputTokens": 0, "cacheCreationEphemeral5mInputTokens": 0,
         "cacheCreationEphemeral1hInputTokens": 0, "webSearchRequests": 0}
    u.update(extra)
    u["costUSD"] = (pricing.calc_cost(model, inp, out, cache_read, 0, DAY) if cost is None else cost)
    return u


def _cs(model_usage, start=T0_MS, total=None):
    return json.dumps({"type": "cost-state", "sessionId": SID, "startTime": start,
                       "totalCostUSD": sum(u["costUSD"] for u in model_usage.values()) if total is None else total,
                       "modelUsage": model_usage})


def out_cost(n, model=MODEL):
    return pricing.calc_cost(model, 0, n, 0, 0, DAY)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects"
        (self.projects / "p").mkdir(parents=True)
        self.file = self.projects / "p" / f"{SID}.jsonl"
        self.db = self.tmp / "whytokens.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *lines, mode="w"):
        with open(self.file, mode, encoding="utf-8") as f:
            f.write("".join(l + "\n" for l in lines))

    def scan(self):
        scan_all(db_path=self.db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(self.projects)])])

    def row(self):
        conn = get_db(self.db)
        try:
            r = conn.execute("SELECT * FROM cost_state WHERE session_id = ?", (SID,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def cmp(self):
        conn = get_db(self.db)
        try:
            return cost_state.compare(conn, SID).get(SID)
        finally:
            conn.close()

    def session(self):
        return next(s for s in api.get_sessions_page(db_path=self.db)["sessions"] if s["full_session_id"] == SID)


class TestIngest(Base):
    def test_last_record_wins(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(100)}), _cs({MODEL: _usage(900)}))
        self.scan()
        r = self.row()
        self.assertAlmostEqual(r["total_cost_usd"], out_cost(900))
        self.assertEqual(r["source_line"], 4)
        self.assertEqual(json.loads(r["model_usage"])[MODEL]["outputTokens"], 900)

    def test_record_appended_after_first_scan_updates_it(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(100)}))
        self.scan()
        self.write(_asst("m2", 50, ts="2026-11-10T10:01:00Z"), _cs({MODEL: _usage(500)}), mode="a")
        self.scan()
        self.assertAlmostEqual(self.row()["total_cost_usd"], out_cost(500))

    def test_new_record_with_no_new_turns_still_updates(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(100)}))
        self.scan()
        self.write(_cs({MODEL: _usage(700)}), mode="a")
        self.scan()
        self.assertAlmostEqual(self.row()["total_cost_usd"], out_cost(700))

    def test_start_after_first_turn_is_partial_and_not_flagged(self):
        late = T0_MS + 90_000  # outside the 60 s slack
        self.write(_prompt(), _asst("m1", 2_000_000), _cs({MODEL: _usage(100)}, start=late))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 0)
        c = self.cmp()
        self.assertEqual(c["cc_coverage"], "partial")
        self.assertIsNone(c["cc_delta_pct"])
        self.assertFalse(c["cc_flag"])
        self.assertEqual(self.session()["cc_coverage"], "partial")

    def test_start_within_the_slack_is_full(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}, start=T0_MS + 30_000))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_start_exactly_at_the_slack_edge_is_full(self):
        # inclusive: start <= first turn + 60 s, compared in whole ms (SQLite's julianday is not exact)
        self.write(_prompt(), _asst("m1", 1000, ts=T0), _cs({MODEL: _usage(1000)}, start=T0_MS + 60_000))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_start_one_ms_past_the_slack_edge_is_partial(self):
        self.write(_prompt(), _asst("m1", 1000, ts=T0), _cs({MODEL: _usage(1000)}, start=T0_MS + 60_001))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 0)

    def test_record_with_no_turns_is_not_shown(self):
        self.write(_cs({MODEL: _usage(1000)}))
        self.scan()
        self.assertIsNone(self.row()["covers_full"])
        self.assertIsNone(self.cmp())

    def test_record_before_the_first_turn_gets_coverage_when_turns_arrive(self):
        self.write(_cs({MODEL: _usage(1000)}))
        self.scan()
        self.write(_prompt(), _asst("m1", 1000), mode="a")
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_first_turn_is_the_earliest_instant_not_the_text_minimum(self):
        # '10:00:00.500Z' sorts before '10:00:00Z' as text; the record starts 60.2 s after 10:00:00
        self.write(_prompt(), _asst("m0", 10, ts="2026-11-10T10:00:00Z"),
                   _asst("m1", 1000, ts="2026-11-10T10:00:00.500Z"),
                   _cs({MODEL: _usage(1000)}, start=T0_MS + 60_200))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 0)

    def test_malformed_numbers_do_not_break_reads(self):
        bad = _usage(1000)
        bad["outputTokens"] = "1000"
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: bad}, start=10**20))
        self.scan()
        self.assertIsNone(self.row()["start_time_ms"])
        api.get_dashboard_data(db_path=self.db)
        self.assertEqual(self.session()["cc_coverage"], "partial")
        conn = get_db(self.db)
        conn.execute("UPDATE cost_state SET covers_full = 1, start_time_ms = ?", (1e300,))
        conn.commit()
        conn.close()
        self.assertEqual(self.cmp()["models"][0]["label"], "priced_differently")

    def test_unreadable_transcript_leaves_the_backfill_for_the_next_scan(self):
        from unittest import mock
        from sources import claude as claude_src
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        conn = get_db(self.db)
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (cost_state.BACKFILL_MARKER,))
        conn.commit()
        with mock.patch.object(claude_src, "last_cost_state", side_effect=PermissionError("locked")):
            ClaudeSource(projects_dirs=[str(self.projects)]).backfill_cost_state(conn)
        self.assertIsNone(scanner._meta_get(conn, cost_state.BACKFILL_MARKER))
        conn.close()

    def test_object_session_id_falls_back_to_the_file_and_keeps_the_turns(self):
        rec = json.loads(_cs({MODEL: _usage(1000)}))
        rec["sessionId"] = {"x": 1}
        self.write(_prompt(), _asst("m1", 1000), json.dumps(rec))
        self.scan()
        self.assertAlmostEqual(self.row()["total_cost_usd"], out_cost(1000))
        conn = get_db(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns WHERE session_id = ?", (SID,)).fetchone()[0], 1)
        conn.close()

    def test_huge_json_int_does_not_break_reads(self):
        bad = _usage(1000)
        bad["outputTokens"] = 10 ** 400
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: bad}))
        self.scan()
        self.assertEqual(self.cmp()["models"][0]["label"], "priced_differently")
        api.get_sessions_page(db_path=self.db)

    def test_unparseable_turn_timestamp_is_not_the_first_turn(self):
        self.write(_prompt(), _asst("m0", 10, ts="garbage"), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_an_earlier_turn_arriving_later_recomputes_coverage(self):
        # the record starts 30 s after the turn seen so far; a turn 90 s before it turns up in another file
        self.write(_prompt(), _asst("m1", 1000, ts="2026-11-10T10:01:00Z"), _cs({MODEL: _usage(1000)}, start=T0_MS + 90_000))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)
        sub = self.projects / "p" / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text(_asst("m0", 10, ts="2026-11-10T10:00:00Z") + "\n", encoding="utf-8")
        self.scan()
        self.assertEqual(self.row()["covers_full"], 0)

    def test_backfill_reads_a_record_only_transcript_on_an_upgraded_db(self):
        self.write(_cs({MODEL: _usage(1000)}))
        self.scan()
        conn = get_db(self.db)
        conn.execute("DELETE FROM cost_state")
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (cost_state.BACKFILL_MARKER,))
        conn.commit()
        conn.close()
        self.write(_prompt(), _asst("m1", 1000), mode="a")
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    @unittest.skipUnless(hasattr(__import__("sqlite3").Connection, "autocommit"), "autocommit= needs Python 3.12+")
    def test_compare_ends_its_own_transaction_under_autocommit(self):
        import sqlite3
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        conn = sqlite3.connect(self.db, autocommit=True)
        conn.row_factory = sqlite3.Row
        cost_state.compare(conn, SID)
        self.assertFalse(conn.in_transaction)
        conn.close()

    def test_subagent_files_are_not_read(self):
        sub = self.projects / "p" / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.jsonl").write_text(_cs({MODEL: _usage(5)}) + "\n", encoding="utf-8")
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        self.assertAlmostEqual(self.row()["total_cost_usd"], out_cost(1000))

    def test_backfill_fills_an_existing_db_once(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        conn = get_db(self.db)
        conn.execute("DELETE FROM cost_state")
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (cost_state.BACKFILL_MARKER,))
        conn.commit()
        conn.close()
        self.scan()
        self.assertAlmostEqual(self.row()["total_cost_usd"], out_cost(1000))

    def test_backfill_without_a_transcript_dir_leaves_the_marker_unset(self):
        conn = get_db(self.db)
        scanner.init_db(conn)
        ClaudeSource(projects_dirs=[str(self.tmp / "missing")]).backfill_cost_state(conn)
        self.assertIsNone(scanner._meta_get(conn, cost_state.BACKFILL_MARKER))
        conn.close()


class TestCompare(Base):
    def test_bracket_suffix_joins_the_base_model(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL + "[1m]": _usage(1000)}))
        self.scan()
        c = self.cmp()
        self.assertEqual([m["model"] for m in c["models"]], [MODEL])
        self.assertAlmostEqual(c["models"][0]["wt_cost"], out_cost(1000))
        self.assertEqual(c["models"][0]["label"], "clean")

    def test_reprice_mismatch_is_priced_differently_and_not_flagged(self):
        # whytokens is 2x what Claude Code charged, but Claude Code's own price is off: a known cause
        self.write(_prompt(), _asst("m1", 2_000_000), _cs({MODEL: _usage(1_000_000, cost=out_cost(1_000_000) * 1.02)}))
        self.scan()
        c = self.cmp()
        self.assertEqual(c["models"][0]["label"], "priced_differently")
        self.assertFalse(c["cc_flag"])

    def test_reprice_inside_the_tolerance_is_clean(self):
        self.write(_prompt(), _asst("m1", 1_000_000), _cs({MODEL: _usage(1_000_000, cost=out_cost(1_000_000) * 1.005)}))
        self.scan()
        self.assertEqual(self.cmp()["models"][0]["label"], "clean")

    def test_local_model_charged_by_claude_code_is_priced_differently(self):
        self.write(_prompt(), _asst("m1", 1_000_000), _cs({MODEL: _usage(1_000_000),
                                                         "qwen3-coder-local": _usage(1000, cost=0.5, model="x")}))
        self.scan()
        labels = {m["model"]: m["label"] for m in self.cmp()["models"]}
        self.assertEqual(labels["qwen3-coder-local"], "priced_differently")

    def test_web_searches_are_part_of_the_reprice(self):
        haiku = "claude-haiku-5-5"
        u = _usage(1000, model=haiku, webSearchRequests=12)
        u["costUSD"] += 12 * pricing.WEB_SEARCH_FEE_USD
        self.write(_prompt(), _asst("m1", 1_000_000), _cs({MODEL: _usage(1_000_000), haiku: u}))
        self.scan()
        labels = {m["model"]: m["label"] for m in self.cmp()["models"]}
        self.assertEqual(labels[haiku], "clean")

    def test_haiku_with_cache_read_is_unverifiable(self):
        haiku = "claude-haiku-5-5"
        self.write(_prompt(), _asst("m1", 1_000_000),
                   _cs({MODEL: _usage(1_000_000), haiku: _usage(10, cost=9.0, model=haiku, cache_read=500_000)}))
        self.scan()
        labels = {m["model"]: m["label"] for m in self.cmp()["models"]}
        self.assertEqual(labels[haiku], "unverifiable")

    def test_haiku_without_cache_read_is_repriced(self):
        haiku = "claude-haiku-5-5"
        self.write(_prompt(), _asst("m1", 1_000_000),
                   _cs({MODEL: _usage(1_000_000), haiku: _usage(10, cost=9.0, model=haiku)}))
        self.scan()
        labels = {m["model"]: m["label"] for m in self.cmp()["models"]}
        self.assertEqual(labels[haiku], "priced_differently")

    def test_cache_write_without_the_ttl_split_is_unverifiable(self):
        u = _usage(1_000_000)
        u["cacheCreationInputTokens"] = 1000
        del u["cacheCreationEphemeral5mInputTokens"], u["cacheCreationEphemeral1hInputTokens"]
        self.write(_prompt(), _asst("m1", 1_000_000), _cs({MODEL: u}))
        self.scan()
        self.assertEqual(self.cmp()["models"][0]["label"], "unverifiable")

    def test_six_percent_gap_with_clean_pricing_is_flagged(self):
        self.write(_prompt(), _asst("m1", 1_060_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        c = self.cmp()
        self.assertAlmostEqual(c["cc_delta_pct"], 0.06, places=6)
        self.assertTrue(c["cc_flag"])
        s = self.session()
        self.assertTrue(s["cc_flag"])
        self.assertEqual(s["cc_coverage"], "full")
        self.assertAlmostEqual(s["cc_cost"], out_cost(1_000_000), places=4)

    def test_negative_six_percent_gap_is_flagged(self):
        self.write(_prompt(), _asst("m1", 940_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        self.assertTrue(self.cmp()["cc_flag"])

    def test_exact_five_percent_gap_is_not_flagged(self):
        self.write(_prompt(), _asst("m1", 4_750), _cs({MODEL: _usage(5_000)}))
        self.scan()
        self.assertFalse(self.cmp()["cc_flag"])

    def test_gap_just_over_five_percent_is_flagged(self):
        self.write(_prompt(), _asst("m1", 1_050_000_001), _cs({MODEL: _usage(1_000_000_000)}))  # 5.0000001%
        self.scan()
        self.assertTrue(self.cmp()["cc_flag"])

    def test_time_only_and_offset_timestamps_pick_the_right_first_turn(self):
        # '00:00:00' alone parses as 2000-01-01 in SQLite; it must not be the first turn
        self.write(_prompt(), _asst("m0", 10, ts="00:00:00"), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_date_shaped_but_invalid_timestamp_is_not_the_first_turn(self):
        self.write(_prompt(), _asst("m0", 10, ts="2026-99-99T00:00:00Z"), _asst("m1", 1000), _cs({MODEL: _usage(1000)}))
        self.scan()
        self.assertEqual(self.row()["covers_full"], 1)

    def test_four_percent_gap_is_not_flagged(self):
        self.write(_prompt(), _asst("m1", 1_040_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        self.assertFalse(self.cmp()["cc_flag"])

    def test_gap_under_ten_cents_is_not_flagged(self):
        n = int(0.09 / out_cost(1) )  # Claude Code says about $0.09
        self.write(_prompt(), _asst("m1", 2 * n), _cs({MODEL: _usage(n)}))
        self.scan()
        self.assertFalse(self.cmp()["cc_flag"])

    def test_gap_at_ten_cents_is_flagged(self):
        n = int(0.101 / out_cost(1)) + 1
        self.write(_prompt(), _asst("m1", 2 * n), _cs({MODEL: _usage(n)}))
        self.scan()
        self.assertTrue(self.cmp()["cc_flag"])

    def test_session_without_a_record_shows_none(self):
        self.write(_prompt(), _asst("m1", 1000))
        self.scan()
        s = self.session()
        self.assertEqual((s["cc_cost"], s["cc_coverage"], s["cc_flag"]), (None, "none", False))

    def test_dashboard_rows_carry_the_fields_and_sort_key_exists(self):
        self.write(_prompt(), _asst("m1", 1_060_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        s = next(x for x in api.get_dashboard_data(db_path=self.db)["sessions_all"] if x["full_session_id"] == SID)
        self.assertTrue(s["cc_flag"])
        self.assertEqual(api.get_sessions_page(sort="cc_cost", db_path=self.db)["sort"], "cc_cost")


class TestSurfaces(Base):
    def test_cc_sort_on_a_db_without_the_table_falls_back(self):
        self.write(_prompt(), _asst("m1", 1000))
        self.scan()
        conn = get_db(self.db)
        conn.execute("DROP TABLE cost_state")
        conn.commit()
        conn.close()
        self.assertEqual(api.get_sessions_page(sort="cc_cost", db_path=self.db)["sort"], "recent")

    def test_export_carries_the_same_fields(self):
        self.write(_prompt(), _asst("m1", 1_060_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        s = next(x for x in api.get_export(db_path=self.db)["sessions"] if x["full_session_id"] == SID)
        self.assertEqual((s["cc_coverage"], s["cc_flag"]), ("full", True))


class TestReconcile(Base):
    def test_no_usage_fills_from_cost_state(self):
        self.write(_prompt(), _asst("m1", 1_060_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        r = reconcile.session_check(SID, None, self.db)
        self.assertEqual(r["usage_source"], "cost-state")
        self.assertAlmostEqual(r["usage"], out_cost(1_000_000), places=6)
        self.assertAlmostEqual(r["delta"], out_cost(60_000), places=6)
        self.assertEqual(r["models"][0]["label"], "clean")

    def test_entered_usage_wins(self):
        self.write(_prompt(), _asst("m1", 1_060_000), _cs({MODEL: _usage(1_000_000)}))
        self.scan()
        r = reconcile.session_check(SID, 12.5, self.db)
        self.assertEqual((r["usage"], r["usage_source"]), (12.5, "entered"))

    def test_partial_record_does_not_fill(self):
        self.write(_prompt(), _asst("m1", 1000), _cs({MODEL: _usage(1000)}, start=T0_MS + 120_000))
        self.scan()
        r = reconcile.session_check(SID, None, self.db)
        self.assertEqual((r["usage"], r["usage_source"], r["cc_coverage"]), (None, None, "partial"))


class TestTooltip(unittest.TestCase):
    def test_wording_is_in_the_ui_once(self):
        src = (Path(__file__).resolve().parent.parent / "static" / "modules" / "reconcile.js").read_text(encoding="utf-8")
        self.assertTrue("Neither number reflects enterprise discounts or what your invoice says." in src)
        self.assertEqual(src.count("Claude Code's own running cost for this session"), 1)


if __name__ == "__main__":
    unittest.main()
