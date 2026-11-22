"""test_cost_layers.py - every cost split into recorded / advisor / estimated, plus the
web-search fee. scripts/falsify_cost_layers.py breaks each piece and checks this file goes red.

Fixture, all claude-opus-4-8 ($5 in / $25 out per MTok), hand-priced:
  recorded   m_plain 1M in $5.00 + m_adv 100K out $2.50 + m_ws 1M in $5.00     = $12.50
  advisor    m_adv's advisor iteration, 1M in                                 =  $5.00
  estimated  agent_summary 1M in $5.00 + advisor :est 40K out $1.00
             + 2 successful WebSearch calls on m_ws x $0.01 (a third errored)  =  $6.02
  total                                                                        = $23.52
m_ws is on 2026-09-21; everything else on 2026-09-20. A fifth WebSearch on m_ws is still pending.
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
import reconcile
import sources
import sources.claude

MODEL = "claude-opus-4-8"
LAYERS = {"recorded": 12.50, "advisor": 5.00, "estimated": 6.02}
TOTAL = 23.52
DAY, WS_DAY = "2026-09-20", "2026-09-21"


def _asst(mid, usage, day=DAY, content=None, **extra):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0}
    base.update(usage)
    rec = {"type": "assistant", "sessionId": "s1", "timestamp": f"{day}T10:00:00Z", "cwd": "/home/dev/proj",
           "message": {"id": mid, "type": "message", "role": "assistant", "model": MODEL,
                       "content": content or [{"type": "text", "text": "x"}], "usage": base}}
    rec.update(extra)
    return rec


def _use(i, name):
    return {"type": "tool_use", "id": f"tu{i}", "name": name, "input": {"query": "q"}}


def _result(i, err, day=WS_DAY):
    return {"type": "user", "sessionId": "s1", "timestamp": f"{day}T10:00:01Z", "cwd": "/home/dev/proj",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{i}",
                                                     "content": "r", "is_error": err}]}}


RECORDS = [
    _asst("m_plain", {"input_tokens": 1_000_000}),
    _asst("m_adv", {"output_tokens": 100_000,
                    "iterations": [{"type": "message", "output_tokens": 100_000},
                                   {"type": "advisor_message", "input_tokens": 1_000_000, "output_tokens": 0}]},
          advisorModel=MODEL),
    _asst("m_ws", {"input_tokens": 1_000_000}, day=WS_DAY,
          content=[_use(1, "WebSearch"), _use(2, "WebSearch"), _use(3, "WebSearch"), _use(4, "Read"),
                   _use(5, "WebSearch")]),
    # tu3's result lands the next day (the fee still dates by its turn); tu5 never got one (pending)
    _result(1, False), _result(2, True), _result(3, False, "2026-09-22"), _result(4, False),
]


def _priced(r, day=None):
    """Token cost of one payload row, the way pricing.js prices it (fee excluded)."""
    d = day or r.get("rate_day") or r.get("timestamp") or r.get("day")
    d = d[:10] if d else None
    c = pricing.calc_cost(r["model"], r["input"], r["output"], r["cache_read"], r["cache_creation"], d,
                          cache_creation_1h=r["cache_creation_1h"])
    x = r.get("x")
    return c + (pricing.calc_cost(r["model"], x["input"], x["output"], x["cache_read"], x["cache_creation"], d,
                                  cache_creation_1h=x["cache_creation_1h"]) if x else 0)


def split(rows):
    """{layer: $} of payload rows; the fee always lands in estimated, whatever the row's layer."""
    out = dict.fromkeys(LAYERS, 0.0)
    for r in rows:
        out[r["layer"]] += _priced(r)
        out["estimated"] += r["web_searches"] * pricing.WEB_SEARCH_FEE_USD
    return out


class CostLayersTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp) / "projects" / "-home-dev-proj"
        proj.mkdir(parents=True)
        (proj / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in RECORDS), encoding="utf-8")
        self.db = Path(self.tmp) / "usage.db"
        sources.scan_all(db_path=self.db, verbose=False,
                         sources=[sources.claude.ClaudeSource(projects_dirs=[str(Path(self.tmp) / "projects")])])
        # The two estimated row kinds the scanner synthesises from subagent files / missing iterations.
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            for mid, inp, out in (("agent_summary:s1:agent-x", 1_000_000, 0), ("advisor:m_plain:est", 0, 40_000)):
                c.execute("INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens,"
                          " cache_read_tokens, cache_creation_tokens, cache_creation_1h_tokens, message_id)"
                          " VALUES ('s1', ?, ?, ?, ?, 0, 0, 0, ?)", (f"{DAY}T10:00:02Z", MODEL, inp, out, mid))
            c.commit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertLayers(self, got, msg=None):
        for k, v in LAYERS.items():
            self.assertAlmostEqual(got[k], v, places=4, msg=f"{msg}: {k}")
        self.assertAlmostEqual(sum(got[k] for k in LAYERS), TOTAL, places=4, msg=msg)

    def test_fee_is_single_sourced_and_served(self):
        self.assertEqual(pricing.WEB_SEARCH_FEE_USD, 0.01)
        self.assertEqual(api.pricing_payload()["_fees"], {"web_search_usd": pricing.WEB_SEARCH_FEE_USD})

    def test_dashboard_rows_split(self):
        data = api.get_dashboard_data(db_path=self.db)
        for key in ("daily_by_model", "daily_by_project"):
            self.assertLayers(split(data[key]), key)
            ws = {r["day"]: r["web_searches"] for r in data[key] if r["web_searches"]}
            self.assertEqual(ws, {WS_DAY: 2}, key)  # errored call not charged; dated by its turn
        s = [x for x in data["sessions_all"] if x["session_id"] == "s1"][0]
        self.assertLayers(split(s["by_model"]), "sessions_all")

    def test_sessions_page_and_breakdown_split(self):
        s = api.get_sessions_page(db_path=self.db)["sessions"][0]
        self.assertLayers(split(s["by_model"]), "sessions page")
        self.assertLayers(split(api.get_session_breakdown("s1", db_path=self.db)["rows"]), "breakdown")

    def test_daily_endpoint(self):
        d = api.get_daily(db_path=self.db)
        self.assertAlmostEqual(d["totals"]["cost"], TOTAL, places=4)
        self.assertLayers(d["totals"]["layers"], "daily totals")
        self.assertEqual(d["totals"]["web_searches"], 2)
        day = [r for r in d["daily"] if r["day"] == WS_DAY]
        self.assertAlmostEqual(sum(r["cost"] for r in day), 5.02, places=4)
        self.assertEqual({k: round(v, 4) for k, v in day[0]["layers"].items()},
                         {"recorded": 5.0, "advisor": 0.0, "estimated": 0.02})
        self.assertLayers({k: sum(r["layers"][k] for r in d["daily"]) for k in LAYERS}, "daily rows")

    def test_tool_effort_and_trace_carry_the_fee(self):
        def total(rows):
            return sum(_priced(r) + r["web_searches"] * pricing.WEB_SEARCH_FEE_USD for r in rows)
        data = api.get_dashboard_data(db_path=self.db)
        for key in ("tool_by_model", "effort_by_model"):
            self.assertAlmostEqual(total(data[key]), TOTAL, places=4, msg=key)
        ws = {r["tool"]: r["web_searches"] for r in data["tool_by_model"] if r["web_searches"]}
        self.assertEqual(ws, {"WebSearch": 2})
        self.assertAlmostEqual(total(api.get_session_detail("s1", db_path=self.db)["turns"]), TOTAL, places=4)

    def test_long_prompt_premium_excludes_the_fee(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE turns SET model = 'claude-haiku-5-5' WHERE message_id = 'm_ws'")
            c.commit()
        with contextlib.closing(api._connect(self.db)) as c:
            tokens = sum(cli.dated_costs(c, f"session_id = 's1' AND {reconcile.LONG_PROMPT}").values())
        self.assertGreater(tokens, 0)
        r = reconcile.session_check("s1", db_path=self.db)
        self.assertAlmostEqual(r["haiku_5x"], tokens * (1 - 1 / pricing.LONG_PROMPT_MULT), places=6)
        self.assertEqual(r["layers"]["web_searches"], 2)

    def test_cli_layers_and_stats(self):
        with contextlib.closing(api._connect(self.db)) as c:
            got = cli.cost_layers(c)
        self.assertLayers(got, "cli.cost_layers")
        self.assertAlmostEqual(got["total"], TOTAL, places=4)
        out = io.StringIO()
        old = cli.DB_PATH
        cli.DB_PATH = self.db
        try:
            with contextlib.redirect_stdout(out):
                cli.cmd_stats()
        finally:
            cli.DB_PATH = old
        self.assertIn(f"Est. total cost:  ${TOTAL:.4f}", out.getvalue())
        self.assertIn("recorded $12.5000 · advisor $5.0000 · estimated $6.0200", out.getvalue())

    def test_reconcile_session_layers(self):
        r = reconcile.session_check("s1", db_path=self.db)
        self.assertAlmostEqual(r["whytokens"], TOTAL, places=4)
        self.assertLayers(r["layers"], "session_check")
        self.assertEqual(r["layers"]["web_searches"], 2)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dashboard_modules_parse(self):
        """The suite evals slices of app.js; a syntax error elsewhere in it would only show in the browser."""
        for f in sorted((Path(pricing.__file__).parent / "static" / "modules").glob("*.js")):
            r = subprocess.run(["node", "--input-type=module", "--check"], input=f.read_text(encoding="utf-8"),
                               capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr[:300]}")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dashboard_js_layers(self):
        """pricing.js costLayers / rowCost / sessionCost on the real payload."""
        data = api.get_dashboard_data(db_path=self.db)
        s1 = [x for x in data["sessions_all"] if x["session_id"] == "s1"][0]
        mjs = Path(self.tmp) / "pricing.mjs"
        shutil.copy(Path(pricing.__file__).parent / "static" / "modules" / "pricing.js", mjs)
        (Path(self.tmp) / "payload.json").write_text(json.dumps(
            {"pricing": api.pricing_payload(), "rows": data["daily_by_model"], "session": s1}))
        script = (
            "import * as P from './pricing.mjs'; import fs from 'fs';"
            "const d = JSON.parse(fs.readFileSync('payload.json', 'utf8'));"
            "P.applyDefaultPricing(d.pricing);"
            "const total = d.rows.reduce((a, r) => a + P.rowCost(r, r.rate_day || r.day), 0);"
            "const L = P.costLayers(d.rows, r => r.rate_day || r.day);"
            "console.log(JSON.stringify([total, P.sessionCost(d.session, '" + DAY + "'), L]));")
        out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.tmp,
                             capture_output=True, text=True, check=True).stdout
        total, session, layers = json.loads(out)
        self.assertAlmostEqual(total, TOTAL, places=4)
        self.assertAlmostEqual(session, TOTAL, places=4)
        self.assertLayers(layers, "pricing.js costLayers")
        self.assertAlmostEqual(layers["total"], TOTAL, places=4)
        self.assertEqual(layers["web_searches"], 2)


if __name__ == "__main__":
    unittest.main()
