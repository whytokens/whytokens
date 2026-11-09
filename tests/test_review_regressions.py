"""Review wt4 fixes, run under node against the real pricing.js / app.js source:
overview cache share = drilldown cacheShare; list-rate session cost ignores
browser overrides; context drops tracked per lane, not per actor type.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pricing

_MOD = Path(pricing.__file__).parent / "static" / "modules"
_SRC = (_MOD / "app.js").read_text(encoding="utf-8")
MODEL = "claude-sonnet-5-5"


def _fn(name, nxt):
    return _SRC[_SRC.index("function %s(" % name):_SRC.index(nxt)]


HEAD = """
import * as P from './pricing.mjs'; import fs from 'fs';
const d = JSON.parse(fs.readFileSync('payload.json', 'utf8'));
P.applyDefaultPricing(d.pricing);
P.loadPricing();
const { sessionCostBreakdown } = P;
%(SRC)s
const eq = (got, want, what) => { if (!(Math.abs(got - want) <= 1e-9)) throw new Error(what + ': got ' + got + ', want ' + want); };
"""

SEGS = {}
# output tokens must not dilute the overview share
SEGS["TN1"] = """
const row = { model: d.model, last_date: '2026-10-20', input: 100, output: 1000, cache_read: 800, cache_creation: 100 };
const agg = aggregateCostBreakdown([row]);
eq(agg.cache_tokens / agg.prompt, P.cacheShare(row), 'overview share == drilldown share');
"""
# a browser override must not move the list-rate cost
SEGS["TN30"] = """
const s = { model: d.model, last_date: '2026-10-20', input: 1e6, output: 0, cache_read: 0, cache_creation: 0 };
const before = P.sessionCostAtListRates(s, s.last_date);
P.setOverride(d.model, 'input', 99);
eq(P.sessionCost(s, s.last_date), 99, 'override applies to normal cost');
eq(P.sessionCostAtListRates(s, s.last_date), before, 'list-rate cost ignores override');
eq(P.sessionCost(s, s.last_date), 99, 'override restored afterwards');
"""
# two Explore agents must not look like a drop; a real drop in one must show
SEGS["TN33"] = """
const t = (actor, lane, context) => ({ actor, lane, context });
const turns = [t('Explore', 'a', 1000), t('Explore', 'b', 100), t('Explore', 'a', 1100), t('Explore', 'b', 120),
               t('Explore', 'a', 300)];
eq(timelineDrops(turns).join() === '4' ? 0 : 1, 0, 'only lane a really dropped: ' + timelineDrops(turns));
"""


class TestReviewWt4(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js(self):
        src = _fn("aggregateCostBreakdown", "function sessionSignals(") + _fn("timelineDrops", "// The x range to show")
        for name, seg in SEGS.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                shutil.copy(_MOD / "pricing.js", Path(tmp) / "pricing.mjs")
                (Path(tmp) / "payload.json").write_text(json.dumps({"pricing": pricing.PRICING, "model": MODEL}))
                out = subprocess.run(["node", "--input-type=module", "-e", (HEAD % {"SRC": src}) + seg],
                                     cwd=tmp, capture_output=True, text=True, timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
