"""cache share counts reads only, savings net the write cost, cold restarts are named.
Runs the real pricing.js under node against prices read from pricing.py.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pricing

_JS = Path(pricing.__file__).parent / "static" / "modules" / "pricing.js"
MODEL = "claude-sonnet-5-5"
DAY = "2026-10-20"

SCRIPT = """
import * as P from './pricing.mjs'; import fs from 'fs';
const d = JSON.parse(fs.readFileSync('payload.json', 'utf8'));
P.applyDefaultPricing(d.pricing);
const t = (actor, gap_s, cc, cc1h) => ({ actor, gap_s, model: d.model, timestamp: d.day, input: 0, output: 0,
  cache_read: 0, cache_creation: cc, cache_creation_1h: cc1h });
const turns = [
  t('main', null, 100000, 100000),   // session start: no gap, not a restart
  t('main', 7000, 177000, 177000),   // idle past the 1h TTL: cold
  t('main', 600, 5000, 5000),        // under 1h, written at 1h: warm
  t('main', 400, 5000, 0),           // over 5m, written at 5m: cold
  t('main', 9999, 0, 0),             // nothing rewritten: not a restart
  t('reviewer', 9999, 50000, 0),     // subagent: not the main thread
];
console.log(JSON.stringify({
  share: P.cacheShare({ input: 100, cache_read: 800, cache_creation: 100 }),
  // writes only, no reads: the old formula said 100%
  shareWrites: P.cacheShare({ input: 0, cache_read: 0, cache_creation: 5000 }),
  savings: P.calcCostBreakdown(d.model, 0, 0, 1e6, 1e6, d.day, 1e6).cache_savings,
  savings5m: P.calcCostBreakdown(d.model, 0, 0, 0, 1e6, d.day, 0).cache_savings,
  cold: P.coldRestarts(turns),
}));
"""


class TestCacheAccounting(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js(self):
        p = pricing.get_pricing(MODEL, DAY)
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(_JS, Path(tmp) / "pricing.mjs")
            (Path(tmp) / "payload.json").write_text(json.dumps(
                {"pricing": pricing.PRICING, "model": MODEL, "day": DAY}))
            out = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], cwd=tmp,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertAlmostEqual(r["share"], 0.8)
        self.assertEqual(r["shareWrites"], 0)
        # 1M reads saved (input - read); 1M 1h writes cost (write_1h - input) extra
        self.assertAlmostEqual(r["savings"], (p["input"] - p["cache_read"]) - (p["cache_write_1h"] - p["input"]), places=6)
        self.assertAlmostEqual(r["savings5m"], -(p["cache_write"] - p["input"]), places=6)
        # independent of the table's own fields: published multipliers on base input. A model with a
        # single flat tier, since sonnet-5-5 cut cache reads to 0.05x on 2026-10-07.
        q = pricing.get_pricing("claude-sonnet-4-5", DAY)
        self.assertAlmostEqual(q["cache_write"], 1.25 * q["input"], places=6)
        self.assertAlmostEqual(q["cache_write_1h"], 2 * q["input"], places=6)
        self.assertAlmostEqual(q["cache_read"], 0.1 * q["input"], places=6)
        self.assertEqual(r["cold"]["count"], 2)
        self.assertEqual(r["cold"]["tokens"], 182000)
        want = (177000 * p["cache_write_1h"] + 5000 * p["cache_write"]) / 1e6
        self.assertAlmostEqual(r["cold"]["cost"], want, places=6)


if __name__ == "__main__":
    unittest.main()
