"""Session timeline: elapsed labels and round, wall-aligned tick steps.

Runs the real frontend functions under node, plus the markup the controls need.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SRC = (_STATIC / "modules" / "app.js").read_text(encoding="utf-8")
_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")

HARNESS = """
let displayTZ = 'utc';
const fmtLocalDateTime = ts => new Date(ts).toISOString().slice(0, 16).replace('T', ' ');
%(SRC)s
const eq = (got, want, what) => { if (got !== want) throw new Error(what + ': got ' + got + ', want ' + want); };
eq(fmtElapsed(0), '0s', '0s');
eq(fmtElapsed(90), '1m30s', '90s');
eq(fmtElapsed(1800), '30m', 'whole minutes drop the seconds');
eq(fmtElapsed(3720), '1h02m', 'hours');
eq(fmtElapsed(93600), '1d02h', 'a resumed session spanning days');
// a 2.5 h session: round half-hour steps from 0, at most 8 steps across
eq(timeTicks(0, 9000, 0).map(t => t.value).join(), '0,1800,3600,5400,7200,9000', 'elapsed steps');
// clock mode: ticks land on wall-clock multiples of the step, not on session start
const origin = 1000, ticks = timeTicks(0, 7200, origin);
eq(ticks[0].value, 800, 'first clock tick');
for (const t of ticks) eq((origin + t.value) %% 900, 0, 'wall-aligned tick ' + t.value);
for (const [lo, hi] of [[0, 30], [0, 600], [100, 100000], [0, 3e6]]) {
  const n = timeTicks(lo, hi, 0).length;
  if (n < 2 || n > 9) throw new Error(lo + '..' + hi + ' gave ' + n + ' ticks');
}
// API-shaped rows: `timestamp` is minute-truncated display text, `ts` the full UTC ISO.
// Positions must come from `ts`: 1 s and 59 s past the minute are 58 s apart, not 0.
const rows = [{ ts: '2026-10-27T10:00:01Z', timestamp: '2026-10-27 10:00' },
              { ts: '2026-10-27T10:00:59Z', timestamp: '2026-10-27 10:00' },
              { ts: '2026-10-27T10:05:00Z', timestamp: '2026-10-27 10:05' }];
eq(timelineXs(rows).join(), '0,58,299', 'x from full timestamps');
// shared timestamps keep their true shared position (timelineCostBars keeps their bars apart)
const same = [{ ts: '2026-10-27T10:00:00Z' }, { ts: '2026-10-27T10:00:00Z' }, { ts: '2026-10-27T10:00:00Z' }];
eq(timelineXs(same).join(), "0,0,0", "coincident calls share their true time");
// an undated first call must not turn every position into NaN
const undated = [{ ts: '' }, { ts: '2026-10-27T10:00:00Z' }, { ts: 'garbage' }, { ts: '2026-10-27T10:01:00Z' }];
const ux = timelineXs(undated);
eq(ux.every(Number.isFinite), true, 'finite positions: ' + ux);
eq(ux.join(), '0,0,0,60', 'undated calls sit at the previous dated one');
eq(timelineOrigin([{ ts: '' }]), undefined, 'no origin without a valid ts');
eq(timelineXs([{ ts: '' }, { ts: null }]).join(), '1,2', 'no dated calls falls back to call numbers');
// drops: per actor, below 60%% of that actor's previous call, at any size
const ctx = [['main', 20000], ['a1', 90000], ['main', 10000], ['a1', 60000], ['main', 9000], ['a1', 30000]]
  .map(([actor, context]) => ({ actor, lane: actor, context }));
eq(timelineDrops(ctx).join(), '2,5', 'context drops');
eq(timelineDrops([{ actor: 'main', context: 5, is_compact_summary: 1 }]).join(), '0', 'flagged compaction');
// positions are true elapsed time, even after a burst of identical timestamps
const burst = Array.from({ length: 1100 }, () => ({ ts: '2026-10-27T10:00:00.000Z' }))
  .concat([{ ts: '2026-10-27T10:00:00.500Z' }]);
eq(timelineXs(burst)[1100], 0.5, 'elapsed after a burst');
// cost bars stack within a call only: two calls at one x keep their own tops
const cb = timelineCostBars([{ cache_creation: 0.02, cache_read: 0.01, output: 0, input: 0 },
                             { cache_creation: 0.003, cache_read: 0, output: 0, input: 0 }], false);
eq(JSON.stringify(cb.cache_creation), '[[0,0.02],[0,0.003]]', 'per-call bases');
eq(JSON.stringify(cb.cache_read), '[[0.02,0.03],null]', 'stacked within the call, zero part skipped');
const lb = timelineCostBars([{ cache_creation: 0.0005, cache_read: 0.0004, output: 0.01, input: 0 }], true);
eq(JSON.stringify([lb.cache_creation[0], lb.cache_read[0], lb.output[0]]), '[null,null,[0.001,0.0109]]', 'log floor');
// axis range covers every call, even when a later row carries an earlier time
eq(JSON.stringify(timelineRange([0, -0.5, 3, 2])), '{"min":-0.5,"max":3}', 'range over all positions');
eq(JSON.stringify(timelineRange([5])), '{"min":5,"max":6}', 'single call gets a 1 s range');
// every call at one x, however many; capped for display with the rest counted
const many = [0, 5, 5, 5, 5, 5, 5, 5, 5, 9];
const at = timelineCallsAt(many, 3, 6);
eq(at.all.length, 8, 'all coincident calls found');
eq(at.shown.join() + '|' + at.more, '1,2,3,4,5,6|2', 'cap and remainder');
eq(timelineCallsAt(many, 0).all.join(), '0', 'a lone call');
eq(fmtElapsed(-0.5), '-0.5s', 'negative elapsed keeps its sign');
// sub-second zoom still gets labelled ticks
eq(timeTicks(0.2, 0.4, 0).map(t => t.value).join(), '0.2,0.3,0.4', 'sub-second ticks');
eq(timeTicks(0.2, 0.4, 1000, 1).length, 2, 'clock mode narrower than a second keeps its ends');
eq(fmtElapsed(0.3), '0.3s', 'sub-second label');
// gaps come from consecutive DATED calls, so an undated row between them hides nothing
const gapRows = [{ ts: '2026-10-27T10:00:00Z' }, { ts: '' }, { ts: '2026-10-27T10:10:00Z' }];
const gx = timelineXs(gapRows);
eq(JSON.stringify(timelineGaps(gapRows, gx)), JSON.stringify([[gx[0], gx[2]]]), 'gap across undated row');
eq(timelineGaps([{ ts: '2026-10-27T10:00:00Z' }, { ts: '2026-10-27T10:04:59Z' }], [0, 299]).length, 0, 'under 5 min');
// clock ticks keep their seconds when the step is under a minute
timelineX = 'clock';
const clk = timelineTick([{ ts: '2026-10-27T10:00:00Z' }]);
eq([0, 5, 30, 60].map(clk).join(), '10:00,10:00:05,10:00:30,10:01', 'clock labels');
timelineX = 'call';
eq(timelineXs(rows).join(), '1,2,3', 'call-number mode');
console.log('ok');
"""


class TestTimelineTicks(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_labels_and_ticks(self):
        fns = _SRC[_SRC.index("const TIMELINE_GAP_S"):_SRC.index("// Gap bands and the drag-to-zoom rectangle")]
        out = subprocess.run(["node", "-e", HARNESS % {"SRC": fns}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "ok")


class TestTimelineMarkup(unittest.TestCase):
    def test_two_panels_and_controls(self):
        for needle in ('id="chart-session-context"', 'id="chart-session-timeline"',
                       "setTimelineX('elapsed')", "setTimelineX('clock')", "setTimelineX('call')",
                       "setTimelineLog(true)", "resetTimelineZoom()"):
            self.assertIn(needle, _HTML)
        # inline handlers only work if the module exports them to window
        self.assertIn("setTimelineX, setTimelineLog, resetTimelineZoom,", _SRC)


if __name__ == "__main__":
    unittest.main()
