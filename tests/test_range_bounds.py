"""Behavioural check on the dashboard's "Last N days" bounds.

The bounds are open-ended (`day >= start`, no end), so an off-by-one start makes
every "Last N Days" range span N+1 calendar days. Runs the real JS under node.
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "static" / "modules" / "app.js"
        ).read_text(encoding="utf-8")

HARNESS = """
const NOW = %(NOW)s;  // null = the real clock; a number pins `new Date()` to that instant
if (NOW !== null) {
  const Real = Date;
  globalThis.Date = class extends Real {
    constructor(...a) { if (a.length) super(...a); else super(NOW); }
    static now() { return NOW; }
  };
}
let displayTZ = 'local';  // localISODate follows the display timezone
%(SRC)s
// Count the days a bound spans, inclusive of today, by walking it forward —
// independent of the arithmetic getRangeBounds used to build the start date.
function span(startISO) {
  const today = localISODate(new Date());
  const d = new Date(startISO + 'T12:00:00');
  let n = 0;
  while (localISODate(d) <= today) {
    if (++n > 400) throw new Error('runaway walk from ' + startISO);
    d.setDate(d.getDate() + 1);
  }
  return n;
}
// Both display timezones: the span is a count of the calendar days in that TZ,
// so switching to UTC must not change how many days a range covers.
for (const mode of ['local', 'utc']) {
  displayTZ = mode;
  for (const [range, want] of [['7d', 7], ['30d', 30], ['90d', 90]]) {
    const { start, end } = getRangeBounds(range);
    if (end !== null) throw new Error(range + ' must be open-ended, got end=' + end);
    const got = span(start);
    if (got !== want) throw new Error(mode + ' ' + range + ' spans ' + got + ' days, want ' + want);
  }
}
console.log('ok');
"""


def _range_fns(src):
    """The self-contained slice of the frontend that computes range bounds."""
    return src[src.index("function localISODate("):src.index("function readURLRange(")]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestRangeBounds(unittest.TestCase):
    def _run(self, now="null", tz=None):
        script = HARNESS.replace("%(NOW)s", now).replace("%(SRC)s", _range_fns(_SRC))
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                           env={**os.environ, **({"TZ": tz} if tz else {})})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_last_n_days_spans_n_days(self):
        self._run()

    def test_utc_range_across_a_dst_change(self):
        # 00:01 UTC on 2026-11-10 is still 11-09 in New York, and 30 days back is on the far
        # side of the 11-01 clock change: stepping back in local days lands an hour short of
        # UTC midnight, one UTC day early.
        self._run(now="Date.UTC(2026, 10, 10, 0, 1)", tz="America/New_York")
