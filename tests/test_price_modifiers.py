"""test_price_modifiers.py - fast mode (2x) and US-only inference (1.1x).

Both multiply every token class of a turn and stack (2.2x). They were billed at
standard rates. scripts/falsify_price_modifiers.py breaks each piece of the fix
and checks this file goes red.

Fixture, all claude-opus-4-8 ($5 in / $25 out per MTok), hand-priced:
  m_fast_us  1,000,000 in, fast + us   $5.00 x 2.2 = $11.00
  m_fast       100,000 out, fast        $2.50 x 2   =  $5.00
  m_std      1,000,000 in               $5.00       =  $5.00
  advisor    1,000,000 in on m_fast     $5.00 (separate inference: not fast)
  total                                               $26.00
"""

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import api
import cli
import pricing
import scanner
import sources
import sources.claude

MODEL = "claude-opus-4-8"
EXPECTED_TOTAL = 26.00


def _record(mid, usage, **extra):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0}
    base.update(usage)
    rec = {"type": "assistant", "sessionId": "s1", "timestamp": "2026-09-20T10:00:00Z",
           "cwd": "/home/dev/proj",
           "message": {"id": mid, "type": "message", "role": "assistant", "model": MODEL,
                       "content": [{"type": "text", "text": "x"}], "usage": base}}
    rec.update(extra)
    return rec


RECORDS = [
    _record("m_fast_us", {"input_tokens": 1_000_000, "speed": "fast", "inference_geo": "us"}),
    _record("m_fast", {"output_tokens": 100_000, "speed": "fast",
                       "iterations": [{"type": "message", "output_tokens": 100_000},
                                      {"type": "advisor_message", "input_tokens": 1_000_000,
                                       "output_tokens": 0}]},
            advisorModel=MODEL),
    _record("m_std", {"input_tokens": 1_000_000, "speed": "standard"}),
]


class PriceModifierTest(unittest.TestCase):
    RECORDS, TOTAL, DAY = RECORDS, EXPECTED_TOTAL, "2026-09-20"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp) / "projects" / "-home-dev-proj"
        proj.mkdir(parents=True)
        (proj / "s1.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in self.RECORDS), encoding="utf-8")
        self.projects = str(Path(self.tmp) / "projects")
        self.db = Path(self.tmp) / "usage.db"
        self.scan()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def src(self):
        return sources.claude.ClaudeSource(projects_dirs=[self.projects])

    def scan(self):
        sources.scan_all(db_path=self.db, verbose=False, sources=[self.src()])

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def speeds(self):
        with contextlib.closing(self.conn()) as c:
            return dict(c.execute("SELECT message_id, COALESCE(speed, '') FROM turns"))

    # -- the rule itself ---------------------------------------------------

    def test_python_and_sql_multipliers_agree(self):
        c = sqlite3.connect(":memory:")
        cols = {"speed", "inference_geo"}
        for speed in ("", "fast", "standard", None):
            for geo in ("", "us", "not_available", None):
                sql_mult = c.execute(
                    "SELECT " + pricing.price_mult_sql(cols).replace("speed", "?", 1)
                    .replace("inference_geo", "?", 1), (speed, geo)).fetchone()[0]
                self.assertAlmostEqual(sql_mult, pricing.price_mult(speed, geo), msg=(speed, geo))
        self.assertAlmostEqual(pricing.price_mult("fast", "us"), 2.2)

    def test_db_without_speed_column_still_prices_us(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE turns (input_tokens, inference_geo)")
        c.execute("INSERT INTO turns VALUES (1000, 'us')")
        cols = {"input_tokens", "inference_geo"}
        self.assertAlmostEqual(
            c.execute(f"SELECT {pricing.price_mult_sql(cols)} FROM turns").fetchone()[0], 1.1)
        row = c.execute(f"SELECT {pricing.surcharge_sql(cols)} FROM turns").fetchone()
        self.assertAlmostEqual(row[0], 100)  # x_input: the 0.1x premium on 1000 tokens

    # -- scanner -----------------------------------------------------------

    def test_scan_records_speed_and_advisor_is_not_fast(self):
        s = self.speeds()
        self.assertEqual(s["m_fast_us"], "fast")
        self.assertEqual(s["m_fast"], "fast")
        self.assertEqual(s["m_std"], "standard")
        advisor = [v for k, v in s.items() if k.startswith(scanner.ADVISOR_ID_PREFIX)]
        self.assertEqual(advisor, [""])

    def test_backfill_fills_rows_ingested_before_the_column(self):
        with contextlib.closing(self.conn()) as c:
            c.execute("UPDATE turns SET speed = NULL")
            c.execute("DELETE FROM schema_meta WHERE key = 'speed_backfill_done_claude'")
            c.commit()
            self.assertEqual(self.src().backfill_speed(c), 2)
        s = self.speeds()
        self.assertEqual((s["m_fast_us"], s["m_fast"], s["m_std"]), ("fast", "fast", ""))

    def test_upsert_rewrites_speed_of_a_row_frozen_mid_stream(self):
        with contextlib.closing(self.conn()) as c:
            c.execute("UPDATE turns SET speed = '', output_tokens = 1 WHERE message_id = 'm_fast'")
            c.execute("DELETE FROM processed_files")
            c.commit()
        self.scan()
        self.assertEqual(self.speeds()["m_fast"], "fast")

    # -- every cost path ---------------------------------------------------

    def row_total(self, rows, day=None):
        return sum(pricing.calc_cost(r["model"], r["input"], r["output"], r["cache_read"],
                                     r["cache_creation"], day or r.get("day"),
                                     cache_creation_1h=r["cache_creation_1h"]) +
                   (pricing.calc_cost(r["model"], r["x"]["input"], r["x"]["output"],
                                      r["x"]["cache_read"], r["x"]["cache_creation"],
                                      day or r.get("day"),
                                      cache_creation_1h=r["x"]["cache_creation_1h"])
                    if r["x"] else 0) for r in rows)

    def test_daily_endpoint(self):
        d = api.get_daily(db_path=self.db)
        self.assertAlmostEqual(d["totals"]["cost"], self.TOTAL, places=4)

    def test_dashboard_rows(self):
        data = api.get_dashboard_data(db_path=self.db)
        for key in ("daily_by_model", "tool_by_model", "effort_by_model"):
            self.assertAlmostEqual(self.row_total(data[key]), self.TOTAL, places=4, msg=key)
        s = [x for x in data["sessions_all"] if x["session_id"] == "s1"][0]
        self.assertAlmostEqual(self.row_total(s["by_model"], self.DAY), self.TOTAL,
                               places=4)

    def test_session_detail_and_breakdown(self):
        turns = api.get_session_detail("s1", db_path=self.db)["turns"]
        self.assertAlmostEqual(self.row_total(turns, self.DAY), self.TOTAL, places=4)
        rows = api.get_session_breakdown("s1", db_path=self.db)["rows"]
        self.assertAlmostEqual(self.row_total(rows, self.DAY), self.TOTAL, places=4)

    def test_live_session(self):
        """get_live_session prices straight from the transcript, no DB."""
        from unittest import mock
        path = str(Path(self.projects) / "-home-dev-proj" / "s1.jsonl")
        with mock.patch.object(api, "_live_transcripts", return_value=("s1", [path])):
            live = api.get_live_session("s1")
        self.assertAlmostEqual(live["cost"], self.TOTAL, places=4)

    def test_cli_stats(self):
        out = io.StringIO()
        old = cli.DB_PATH
        cli.DB_PATH = self.db
        try:
            with contextlib.redirect_stdout(out):
                cli.cmd_stats()
        finally:
            cli.DB_PATH = old
        self.assertIn(f"Est. total cost:  ${self.TOTAL:.4f}", out.getvalue())

    def test_waste_prices_wasted_tokens_at_the_turn_multiplier(self):
        import waste
        with contextlib.closing(self.conn()) as c:
            row = c.execute(
                f"SELECT model, timestamp, cache_creation_1h_tokens, "
                f"{pricing.price_mult_sql({'speed', 'inference_geo'})} AS mult "
                f"FROM turns WHERE message_id = 'm_fast_us'").fetchone()
        self.assertAlmostEqual(waste._cost(row, inp=1_000_000), 11.00, places=4)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dashboard_js_prices_the_surcharge(self):
        """pricing.js rowCost / sessionCost, run under node on the real payload."""
        data = api.get_dashboard_data(db_path=self.db)
        s1 = [x for x in data["sessions_all"] if x["session_id"] == "s1"][0]
        mjs = Path(self.tmp) / "pricing.mjs"
        shutil.copy(Path(pricing.__file__).parent / "static" / "modules" / "pricing.js", mjs)
        (Path(self.tmp) / "payload.json").write_text(json.dumps(
            {"pricing": pricing.PRICING, "rows": data["daily_by_model"], "session": s1}))
        script = (
            "import * as P from './pricing.mjs'; import fs from 'fs';"
            "const d = JSON.parse(fs.readFileSync('payload.json', 'utf8'));"
            "P.applyDefaultPricing(d.pricing);"
            "const rows = d.rows.reduce((a, r) => a + P.rowCost(r, r.day), 0);"
            "console.log(JSON.stringify([rows, P.sessionCost(d.session, '" + self.DAY + "')]));")
        out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.tmp,
                             capture_output=True, text=True, check=True).stdout
        rows, session = json.loads(out)
        self.assertAlmostEqual(rows, self.TOTAL, places=4)
        self.assertAlmostEqual(session, self.TOTAL, places=4)


# Haiku 5.5 long prompts (5x once input + cache_read + cache_creation > 100K),
# hand-priced at $0.10 in / $0.50 out / $0.01 cache read per MTok:
#   h_long   1 in + 100,000 cr = 100,001 prompt, 1M out   $0.5010001 x 5 = $2.5050005
#   h_edge   100,000 cr = exactly 100K (not over), 1M out  $0.501
#   h_old    claude-haiku-4-5, 200,000 cr (no long tier)   $0.02
#   h_cc     90,001 cr + 10,000 cache write = 100,001     $0.00215001 x 5 = $0.01075005
LONG_TOTAL = 3.03675055


def _haiku(mid, model, usage):
    rec = _record(mid, usage)
    rec["timestamp"] = "2026-10-07T10:00:00Z"
    rec["message"]["model"] = model
    return rec


LONG_RECORDS = [
    _haiku("h_long", "claude-haiku-5-5", {"input_tokens": 1, "cache_read_input_tokens": 100_000,
                                          "output_tokens": 1_000_000}),
    _haiku("h_edge", "claude-haiku-5-5", {"cache_read_input_tokens": 100_000,
                                          "output_tokens": 1_000_000}),
    _haiku("h_old", "claude-haiku-4-5", {"cache_read_input_tokens": 200_000}),
    _haiku("h_cc", "claude-haiku-5-5", {"cache_read_input_tokens": 90_001,
                                        "cache_creation_input_tokens": 10_000}),
]


class LongPromptTest(PriceModifierTest):
    """Same end-to-end paths as PriceModifierTest, on a Haiku 5.5 fixture."""
    RECORDS, TOTAL, DAY = LONG_RECORDS, LONG_TOTAL, "2026-10-07"

    def test_python_and_sql_multipliers_agree(self):
        with contextlib.closing(self.conn()) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(turns)")}
            for r in c.execute(f"SELECT *, {pricing.price_mult_sql(cols)} AS mult FROM turns"):
                py = pricing.price_mult(r["speed"], r["inference_geo"], r["model"],
                                        r["input_tokens"] + r["cache_read_tokens"]
                                        + r["cache_creation_tokens"])
                self.assertAlmostEqual(r["mult"], py, msg=r["message_id"])
                self.assertEqual(py, 5.0 if r["message_id"] in ("h_long", "h_cc") else 1.0)

    # Fast-mode / US-geo specifics don't apply to this fixture.
    test_db_without_speed_column_still_prices_us = None
    test_scan_records_speed_and_advisor_is_not_fast = None
    test_backfill_fills_rows_ingested_before_the_column = None
    test_upsert_rewrites_speed_of_a_row_frozen_mid_stream = None
    test_waste_prices_wasted_tokens_at_the_turn_multiplier = None

class DatedTierAggregateTest(unittest.TestCase):
    """CLI aggregates must price each turn at ITS date's tier: Sonnet 5.5 cache
    reads were $0.20 through 2026-10-06 and $0.10 from 2026-10-07, so 1M on each
    day is $0.30 — not $0.20 (both at today's rate) or $0.40."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp) / "projects" / "-home-dev-proj"
        proj.mkdir(parents=True)
        recs = []
        for mid, ts in (("d6", "2026-10-06T12:00:00Z"), ("d7", "2026-10-07T12:00:00Z")):
            r = _record(mid, {"cache_read_input_tokens": 1_000_000})
            r["timestamp"], r["message"]["model"] = ts, "claude-sonnet-5-5"
            recs.append(r)
        (proj / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs),
                                       encoding="utf-8")
        self.db = Path(self.tmp) / "usage.db"
        sources.scan_all(db_path=self.db, verbose=False, sources=[
            sources.claude.ClaudeSource(projects_dirs=[str(Path(self.tmp) / "projects")])])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stats_prices_each_day_at_its_tier(self):
        old, out = cli.DB_PATH, io.StringIO()
        cli.DB_PATH = self.db
        try:
            with contextlib.redirect_stdout(out):
                cli.cmd_stats()
        finally:
            cli.DB_PATH = old
        self.assertIn("Est. total cost:  $0.3000", out.getvalue())

if __name__ == "__main__":
    unittest.main()
