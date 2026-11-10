"""Waste tab view: findings filtered to the sessions the other tabs show, grouped
by project x cause, with titles. Runs the real groupWaste() under node, plus the markup.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SRC = (_STATIC / "modules" / "app.js").read_text(encoding="utf-8")
_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")

HARNESS = """
%(SRC)s
const esc = s => String(s), fmtPct = v => (v * 100).toFixed(1) + '%%', fmtCostBig = c => '$' + c.toFixed(2);
const addSplit = (acc, f) => { acc[f.attribution] += f.usd; };  // provenance.js (tested in test_provenance)
const eq = (got, want, what) => { if (got !== want) throw new Error(what + ': got ' + got + ', want ' + want); };
const f = (sid, cause, usd, session_usd) => ({ session_id: sid, cause, usd, session_usd, tier: 'attributed cause', attribution: cause === 'resident_output' && sid === 's1' ? 'inferred' : 'observed',
                                               detail: cause + ' in ' + sid, fix: 'fix ' + cause, evidence: [] });
// listed out of order on purpose: the view must sort, not inherit the API's order
const findings = [
  f('s3', 'idle_gap', 5, 20), f('s2', 'idle_gap', 2, 4),
  f('s1', 'idle_gap', 3, 10), f('s1', 'resident_output', 1, 10),
  f('s9', 'idle_gap', 50, 60),   // a session outside the current filters
];
const sessions = [
  { full_session_id: 's1', project: 'zeta', topic: 'Fix login', last_date: '2026-10-20' },
  { full_session_id: 's2', project: 'zeta', topic: null, last_date: '2026-10-21' },
  { full_session_id: 's3', project: 'alpha', topic: 'Ship it', last_date: '2026-10-22' },
];
const v = groupWaste(findings, sessions);
// scope: only filtered sessions count; affected spend counts each session once
eq(v.total, 11, 'attributed total');
eq(v.affectedUsd, 34, 'spend of affected sessions, once each');
eq(v.affectedSessions, 3, 'affected sessions');
// cause bars, biggest first
eq(v.causes.map(c => c.cause + ':' + c.usd + ':' + c.sessions).join(), 'idle_gap:10:3,resident_output:1:1', 'causes');
eq(v.causes.map(c => c.share.toFixed(4)).join(), (10 / 11).toFixed(4) + ',' + (1 / 11).toFixed(4), 'cause shares');
// a mixed-source cause keeps observed and inferred dollars apart
const ro = groupWaste([f('s1', 'resident_output', 1, 10), {...f('s2', 'resident_output', 4, 4), attribution: 'inferred'}], sessions);
eq(ro.causes[0].observed + '/' + ro.causes[0].inferred, '0/5', 'split');
eq(v.causes[0].observed + '/' + v.causes[0].inferred, '10/0', 'observed cause');
// habits: (project, cause), biggest first, sessions biggest first, titles joined
eq(v.groups.map(g => g.project + '/' + g.cause + ':' + g.usd).join(),
   'zeta/idle_gap:5,alpha/idle_gap:5,zeta/resident_output:1', 'groups (tie: more sessions first, not by name)');
const a = v.groups.find(g => g.project === 'zeta' && g.cause === 'idle_gap');
eq(a.sessions.map(s => s.id).join(), 's1,s2', 'sessions in a habit');
eq(a.sessions[0].title, 'Fix login', 'title joined');
eq(a.sessions[1].title, '', 'missing topic is empty, not "null"');
eq(a.sessions[0].date, '2026-10-20', 'date joined');
eq(a.spend, 14, 'spend of the sessions in a habit');
eq(a.fix, 'fix idle_gap', 'fix carried to the habit');
// cause filter narrows groups but not the bars
const only = groupWaste(findings, sessions, 'resident_output');
eq(only.groups.map(g => g.cause).join(), 'resident_output', 'cause filter');
eq(only.causes.length, 2, 'bars keep every cause');
eq(only.total, 11, 'scope keeps every cause');
eq(only.cause, 'resident_output', 'applied filter reported');
// a filtered cause that left the scope stops filtering instead of hiding everything
const gone = groupWaste(findings, sessions, 'repeated_reads');
eq(gone.cause, null, 'out-of-scope cause dropped');
eq(gone.groups.length, 3, 'all habits shown again');
// nothing in scope
eq(groupWaste(findings, []).groups.length, 0, 'empty');
// weekly sparkline: share of ALL spend that week, Monday weeks, gap weeks filled
const ws = [
  { full_session_id: 'a', last_date: '2026-10-19', cost: 100 },   // Monday
  { full_session_id: 'b', last_date: '2026-10-25', cost: 100 },   // Sunday, same week
  { full_session_id: 'c', last_date: '2026-11-04', cost: 50 },    // two weeks later
];
const wf = [{ session_id: 'a', usd: 10 }, { session_id: 'c', usd: 5 }, { session_id: 'zz', usd: 99 }];
const wk = weeklyWaste(wf, ws, s => s.cost);
eq(wk.map(w => w.week).join(), '2026-10-19,2026-10-26,2026-11-02', 'weeks, gap filled');
eq(wk.map(w => w.spend).join(), '200,0,50', 'spend counts unaffected sessions too');
eq(wk.map(w => w.pct).join(), '0.05,0,0.1', 'pct of all spend; out-of-scope finding ignored');
eq(weeklyWaste([], [], s => 0).length, 0, 'empty weeks');
eq(wasteSparkline(wk).includes('<polyline'), true, 'sparkline drawn');
eq(wasteSparkline(wk.slice(0, 1)), '', 'one week: no sparkline');
console.log('ok');
"""


class TestGroupWaste(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_grouping(self):
        fns = _SRC[_SRC.index("function groupWaste("):_SRC.index("let wastePayload")]
        out = subprocess.run(["node", "-e", HARNESS % {"SRC": fns}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "ok")


class TestWasteMarkup(unittest.TestCase):
    def test_sections_and_controls(self):
        for needle in ('id="waste-scope"', 'id="waste-bars"', 'id="waste-groups"', 'id="waste-measure"'):
            self.assertTrue(needle in _HTML, needle)
        self.assertTrue("setWasteCause," in _SRC, "setWasteCause export")  # inline handlers need the window export
        # range buttons only: setRange must not clear the timeline's own toggle buttons
        self.assertEqual(_SRC.count("querySelectorAll('.range-btn[data-range]')"), 2, "both range toggles scoped")
        self.assertFalse("querySelectorAll('.range-btn')" in _SRC, "an unscoped .range-btn toggle remains")


if __name__ == "__main__":
    unittest.main()
