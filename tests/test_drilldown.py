"""Dashboard drilldown: clickable Overview cards, the where-to-act strip, URL-addressable
Sessions/Waste state, the Sessions cause filter and Overhead column, and the Trace findings panel.

Pure frontend logic (static/modules/drill.js, slices of app.js) runs under node; the server half
(GET /api/waste?session_id=) runs against the fixture DB. scripts/falsify_drilldown.py breaks each
piece and checks this file goes red.
"""

import json
import shutil
import subprocess
import threading
import unittest
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import api
from tests.test_waste import _Scanned

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "static" / "modules" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DRILL_URL = (ROOT / "static" / "modules" / "drill.js").as_uri()
NODE = shutil.which("node")


def _node(body):
    out = subprocess.run([NODE, "--input-type=module", "-e", body], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _slice(start, end):
    a = APP.index(start)
    return APP[a:APP.index(end, a + 1)]


class TestSessionFindingsApi(_Scanned):
    def test_one_session_matches_its_rows_of_the_full_report(self):
        full = api.get_waste(db_path=self.db)["findings"]
        sid = full[0]["session_id"]
        mine = api.get_waste(db_path=self.db, session_id=sid)
        self.assertEqual(mine["session_id"], sid)
        want = [(f["cause"], f["usd"]) for f in full if f["session_id"] == sid]
        self.assertEqual([(f["cause"], f["usd"]) for f in mine["findings"]], want)
        self.assertEqual({f["session_id"] for f in mine["findings"]}, {sid})
        self.assertAlmostEqual(mine["total_usd"], sum(u for _, u in want))
        self.assertEqual(mine["basis"]["pricing"], "API list rate")

    def test_every_session_with_findings_is_covered(self):
        full = api.get_waste(db_path=self.db)["findings"]
        for sid in {f["session_id"] for f in full}:
            got = api.get_waste(db_path=self.db, session_id=sid)["findings"]
            self.assertEqual(len(got), len([f for f in full if f["session_id"] == sid]), sid)

    def test_clean_and_unknown_sessions_have_no_findings(self):
        self.assertEqual(api.get_waste(db_path=self.db, session_id="clean")["findings"], [])
        self.assertEqual(api.get_waste(db_path=self.db, session_id="no-such-session")["findings"], [])

    def test_http_route_reads_session_id(self):
        full = api.get_waste(db_path=self.db)["findings"]
        sid = full[0]["session_id"]
        old = api.DB_PATH
        api.DB_PATH = Path(self.db)
        server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}/api/waste"
            with urllib.request.urlopen(base + "?session_id=" + sid) as r:
                one = json.loads(r.read())
            with urllib.request.urlopen(base) as r:
                whole = json.loads(r.read())
        finally:
            server.shutdown()
            server.server_close()
            api.DB_PATH = old
        self.assertEqual({f["session_id"] for f in one["findings"]}, {sid})
        self.assertIn("rollup", whole)
        self.assertGreater(len({f["session_id"] for f in whole["findings"]}), 1)


@unittest.skipUnless(NODE, "node not installed")
class TestDrillModule(unittest.TestCase):
    def test_state_round_trips_through_the_url(self):
        self.assertEqual(_node(f"""
import {{ writeSessionState, readSessionState, writeWasteState, readWasteState }} from '{DRILL_URL}';
const f = {{ project: 'acme', branch: 'main', tool: 'Bash', minCost: 1.5, hasCacheCreation: true, cause: 'idle_gap', shape: '', tag: '', minX: 0 }};
const p = new URLSearchParams(); writeSessionState(p, f, {{ col: 'overhead', dir: 'asc' }});
const back = readSessionState(p.toString());
console.log(JSON.stringify([back.filters, back.sort]) === JSON.stringify([f, {{ col: 'overhead', dir: 'asc' }}]) ? 'ok' : 'BAD ' + p);
const d = new URLSearchParams(); writeSessionState(d);
console.log(d.toString() === '' ? 'ok' : 'defaults leaked ' + d);
const w = new URLSearchParams(); writeWasteState(w, 'idle_gap', '2026-11-02');
const ws = readWasteState(w.toString());
console.log(ws.cause === 'idle_gap' && ws.week === '2026-11-02' ? 'ok' : 'BAD waste');
console.log(readWasteState('week=garbage').week === '' ? 'ok' : 'week not validated');
console.log(readSessionState('sort=;drop&dir=up').sort.col === 'last' ? 'ok' : 'unknown sort accepted');
"""), "ok\nok\nok\nok\nok")

    def test_card_links_point_at_the_view_that_explains_the_number(self):
        self.assertEqual(_node(f"""
import {{ CARD_TARGET, drillHref }} from '{DRILL_URL}';
const g = new URLSearchParams('range=7d&models=m1');
const href = l => drillHref(g, {{ tab: 'sessions', sort: CARD_TARGET[l] }});
const want = {{
  'Est. Cost': 'sort=cost&dir=desc', 'Cache Creation': 'sort=cache_creation&dir=desc',
  'Cache Read': 'sort=cache_share&dir=asc', 'Cache Savings': 'sort=cache_share&dir=asc',
}};
for (const [l, q] of Object.entries(want)) {{
  const h = href(l);
  if (!h.includes('tab=sessions') || !h.includes(q) || !h.includes('range=7d') || !h.includes('models=m1')) throw new Error(l + ' -> ' + h);
}}
if (href('Sessions').includes('sort=')) throw new Error('Sessions card should keep the default order');
if (drillHref(g, {{ tab: 'overview' }}).includes('tab=')) throw new Error('overview carries no tab');
const w = drillHref(g, {{ tab: 'waste', cause: 'idle_gap' }});
if (!w.includes('tab=waste') || !w.includes('cause=idle_gap') || !w.includes('range=7d')) throw new Error(w);
const s = drillHref(g, {{ tab: 'sessions', filters: {{ cause: 'idle_gap' }}, sort: {{ col: 'overhead', dir: 'desc' }} }});
if (!s.includes('cause=idle_gap') || !s.includes('sort=overhead&dir=desc')) throw new Error(s);
console.log('ok');
"""), "ok")

    def test_shape_filters_and_peer_sorts_round_trip_through_the_url(self):
        out = _node(f"""
import {{ writeSessionState, readSessionState, EMPTY_FILTERS, DEFAULT_SORT, SESSION_SORTS }} from '{DRILL_URL}';
const eq = (g, w, m) => {{ if (JSON.stringify(g) !== JSON.stringify(w)) throw new Error(m + ': got ' + JSON.stringify(g) + ', want ' + JSON.stringify(w)); }};
const f = {{ ...EMPTY_FILTERS, shape: 'edit', tag: 'client work', minX: 2 }};
const p = new URLSearchParams(); writeSessionState(p, f, {{ col: 'peers', dir: 'desc' }});
eq([p.get('sshape'), p.get('stag'), p.get('sx'), p.get('sort')], ['edit', 'client work', '2', 'peers'], 'params');
const back = readSessionState(p.toString());
eq([back.filters, back.sort], [f, {{ col: 'peers', dir: 'desc' }}], 'round trip');
const d = new URLSearchParams(); writeSessionState(d);
eq(d.toString(), '', 'defaults omitted');
eq(readSessionState('').filters, EMPTY_FILTERS, 'empty url = defaults');
eq(readSessionState('sort=shape&dir=asc').sort, {{ col: 'shape', dir: 'asc' }}, 'shape sort allowed');
eq(readSessionState('sort=vs_peers&sx=abc').sort, DEFAULT_SORT, 'unknown sort falls back');
eq(readSessionState('sx=abc').filters.minX, 0, 'junk multiple tolerated');
console.log('ok');
""")
        self.assertEqual(out, "ok")

    def test_overhead_index_sums_server_dollars(self):
        self.assertEqual(_node(f"""
import {{ overheadIndex, overheadOf }} from '{DRILL_URL}';
const idx = overheadIndex([
  {{ session_id: 'a', cause: 'idle_gap', usd: 1.5 }}, {{ session_id: 'a', cause: 'repeated_reads', usd: 0.5 }}, {{ session_id: 'a', cause: 'idle_gap', usd: 0.5 }},
  {{ session_id: 'b', cause: 'idle_gap', usd: 2 }}]);
const eq = (g, w, m) => {{ if (Math.abs(g - w) > 1e-9) throw new Error(m + ': ' + g); }};
eq(overheadOf(idx, 'a'), 2.5, 'total'); eq(overheadOf(idx, 'a', 'idle_gap'), 2, 'cause share sums repeats');
eq(overheadOf(idx, 'a', 'uncached_input'), 0, 'absent cause'); eq(overheadOf(idx, 'zzz'), 0, 'unknown session');
eq(overheadOf(null, 'a'), 0, 'no payload yet'); console.log('ok');
"""), "ok")


_STUBS = """
const esc = s => String(s), fmt = n => String(n), fmtCostBig = c => '$' + c.toFixed(2), fmtPct = n => (100 * n).toFixed(1) + '%';
const has = (h, n, w) => { if (!h.includes(n)) throw new Error(w + ': missing ' + n + ' in ' + h); };
const lacks = (h, n, w) => { if (h.includes(n)) throw new Error(w + ': unexpected ' + n); };
const counted = f => f.in_total !== false;  // drill.js
"""


@unittest.skipUnless(NODE, "node not installed")
class TestOverviewCards(unittest.TestCase):
    def test_every_card_is_a_link_and_the_overhead_card_goes_to_waste(self):
        src = _slice("function renderStats", "const sessionsByCauseHref")
        out = _node(f"""
import {{ CARD_TARGET, drillHref }} from '{DRILL_URL}';
{_STUBS}
const RANGE_LABELS = {{ '7d': 'Last 7 days' }}, selectedRange = '7d', C = {{ green: 'x' }}, orUnknown = v => v;
const globalParams = () => new URLSearchParams('range=7d');
let html = ''; globalThis.document = {{ getElementById: () => ({{ set innerHTML(v) {{ html = v; }} }}) }};
{src}
renderStats({{ input: 1, output: 1, cache_read: 1, cache_creation: 1, sessions: 3, turns: 9 }},
  {{ total: 10, input: 1, output: 1, cache_read: 1, cache_creation: 1, cache_savings: 5, cache_tokens: 1, prompt: 2 }});
const cards = html.split('<a class="stat-card').slice(1);
if (cards.length !== 8) throw new Error('want 8 linked cards, got ' + cards.length);
if (html.includes('<div class="stat-card')) throw new Error('a dead-end card is left');
const hrefOf = l => {{ const c = cards.find(x => x.includes('>' + l + '<')); return c && /href="([^"]+)"/.exec(c)[1]; }};
for (const l of ['Est. Cost', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Cache Savings', 'Sessions']) {{
  const h = hrefOf(l);
  if (h !== drillHref(new URLSearchParams('range=7d'), {{ tab: 'sessions', sort: CARD_TARGET[l] }})) throw new Error(l + ' -> ' + h);
}}
has(hrefOf('Attributed overhead'), 'tab=waste', 'overhead card');
console.log('ok');
""")
        self.assertEqual(out, "ok")


@unittest.skipUnless(NODE, "node not installed")
class TestWhereToAct(unittest.TestCase):
    def _run(self, findings, scope_ids):
        src = _slice("function groupWaste", "// Weekly attributed waste") + _slice('// Overview "Where to act"', "// Bucket rows into 24 hours")
        return _node(f"""
import {{ drillHref }} from '{DRILL_URL}';
{_STUBS}
const CAUSE_LABEL = {{ a: 'Cause A', b: 'Cause B', c: 'Cause C', d: 'Cause D' }}, avoidTag = () => '';
const addSplit = (x, f) => {{ x[f.attribution] = (x[f.attribution] || 0) + f.usd; }};
const globalParams = () => new URLSearchParams('range=7d');
const wasteCauseHref = cause => drillHref(globalParams(), {{ tab: 'waste', cause }});
const sessionCostAtListRates = s => s.cost;
const findings = {json.dumps(findings)};
const loadWaste = async () => ({{ findings }});
let activeTab = 'overview';
const lastScopeSessions = {json.dumps([{"full_session_id": i, "project": "p", "cost": 100.0, "last_date": "2026-11-01"} for i in scope_ids])};
const slot = {{ innerHTML: '' }}, card = {{ querySelector: q => card[q] || (card[q] = {{ textContent: '' }}) }};
globalThis.document = {{ getElementById: id => id === 'where-to-act' ? slot : card }};
{src}
await renderWhereToAct();
console.log(JSON.stringify({{ slot: slot.innerHTML, value: card['.value'].textContent, sub: card['.sub'].textContent }}));
""")

    def test_top_three_causes_in_scope_link_to_waste(self):
        f = lambda sid, cause, usd: {"session_id": sid, "cause": cause, "usd": usd, "tier": "observed",
                                      "attribution": "observed", "session_usd": 50, "fix": "", "evidence": [], "action": None}
        findings = [f("s1", "a", 1), f("s1", "b", 4), f("s2", "c", 3), f("s2", "d", 2), f("s3", "a", 99), f("s2", "b", 1)]
        got = json.loads(self._run(findings, ["s1", "s2"]))  # s3 is out of scope
        rows = got["slot"].split('<a class="wta-row"')[1:]
        self.assertEqual(len(rows), 3, got["slot"])
        self.assertEqual([r.split("Cause ")[1][0] for r in rows], ["B", "C", "D"])  # biggest first, scope applied
        self.assertIn("tab=waste&cause=b", rows[0])
        self.assertIn("2 sessions", rows[0])  # b spans s1 and s2
        self.assertIn("$5.00", rows[0])
        self.assertIn("lower bound", got["slot"])
        self.assertEqual(got["value"], "$11.00")  # 1+4+3+2+1, not the out-of-scope 99
        self.assertIn("5.5% of cost", got["sub"])  # 11 of 200 list-rate dollars
        self.assertIn("lower bound", got["sub"])

    def test_no_findings_leaves_no_strip(self):
        got = json.loads(self._run([], ["s1"]))
        self.assertEqual(got["slot"], "")
        self.assertEqual(got["value"], "$0.00")


_FINDINGS_HARNESS = _STUBS + """
const CAUSE_LABEL = { resident_output: 'Tool output kept in context', repeated_reads: 'Repeated file reads' };
const avoidTag = () => '', verifySlot = (p, c) => '<div class="wg-verify" data-vcause="' + c + '"></div>';
const recordButton = (p, c, n) => '<button class="wg-record" data-rproject="' + p + '">I made this change</button>';
"""


@unittest.skipUnless(NODE, "node not installed")
class TestSessionFindingsPanel(unittest.TestCase):
    def _run(self, check):
        src = _slice("const CACHE_MISS_CHECKLIST", "// Group findings into what a user acts on")
        return _node(_FINDINGS_HARNESS + src + check)

    def test_states(self):
        out = self._run("""
const ev = [{ message_id: 'm9', at: '2026-11-01T10:00:00Z', tool: 'Read', tokens: 5, usd: 0.5 }];
const act = { id: 'read_ranges', title: 'Read whole files 2 times', rule: 'RULE TEXT', est_usd: 1, est_basis: 'basis',
  calls: [{ message_id: 'm1', at: 't', tool: 'Read', file: 'big.py', result_tokens: 12000, prompt_seq: 7 }] };
const base = { session_id: 's1', detail: 'DETAIL', fix: 'FIX TEXT', evidence: ev, usd: 2.5, action: null };
// no findings: one quiet line, no empty box
const none = sessionFindingsHTML({ findings: [] }, 'proj');
has(none, 'No attributed overhead found in this session.', 'empty'); lacks(none, '<table', 'empty has no table');
has(sessionFindingsHTML(null, 'proj'), 'unavailable', 'failed load');
// a finding with an action: rule, copy, the calls deep-linking to their turns, record button, no full-trace button
const a = sessionFindingsHTML({ findings: [{ ...base, cause: 'repeated_reads', action: act }] }, 'proj');
has(a, 'Repeated file reads', 'cause label'); has(a, '$2.50', 'cause usd'); has(a, 'lower bound', 'basis');
has(a, 'RULE TEXT', 'rule'); has(a, 'Copy', 'copy'); has(a, 'big.py', 'call file');
has(a, 'data-session="s1"', 'call session'); has(a, 'data-turn="m1"', 'call deep link'); has(a, 'prompt 7', 'call label');
has(a, 'I made this change', 'record button'); has(a, 'wg-verify', 'verify slot');
lacks(a, 'Open full trace', 'already in the trace');
// no action: the fix text and the evidence turns, even for the causes the Waste tab calls "no single call"
const n = sessionFindingsHTML({ findings: [{ ...base, cause: 'resident_output' }] }, 'proj');
has(n, 'FIX TEXT', 'fix'); has(n, 'data-turn="m9"', 'evidence turn'); has(n, 'DETAIL', 'detail');
lacks(n, 'No single call stands out', 'no generic fallback in the trace'); lacks(n, 'I made this change', 'no rule to record');
// one block per cause
const two = sessionFindingsHTML({ findings: [{ ...base, cause: 'resident_output' }, { ...base, cause: 'repeated_reads', action: act }] }, 'proj');
if (two.split('class="finding-block"').length !== 3) throw new Error('want two blocks');
console.log('ok');
""")
        self.assertEqual(out, "ok")


@unittest.skipUnless(NODE, "node not installed")
class TestSessionsListLogic(unittest.TestCase):
    def test_cause_filter_and_overhead_and_cache_share_sorts(self):
        src = _slice("function applySessionFilters", "// ── URL persistence") + _slice("function sortSessions", "function aggregateCostBreakdown")
        out = _node(f"""
import {{ overheadIndex, overheadOf }} from '{DRILL_URL}';
{_STUBS}
let sessionFilters = {{ project: '', branch: '', tool: '', minCost: 0, hasCacheCreation: false, cause: '' }};
let sessionSortCol = 'overhead', sessionSortDir = 'desc', activeTab = 'sessions';
let idx = overheadIndex([{{ session_id: 'a', cause: 'idle_gap', usd: 1 }}, {{ session_id: 'a', cause: 'repeated_reads', usd: 5 }},
  {{ session_id: 'b', cause: 'idle_gap', usd: 3 }}]);
const wasteIndex = () => idx, sessionCost = s => s.cost;
const cacheShare = s => s.share;
const S = [{{ full_session_id: 'a', cost: 1, share: 0.9 }}, {{ full_session_id: 'b', cost: 2, share: 0.1 }}, {{ full_session_id: 'c', cost: 3, share: 0.5 }}];
{src}
const ids = l => l.map(s => s.full_session_id).join('');
const eq = (g, w, m) => {{ if (g !== w) throw new Error(m + ': got ' + g + ', want ' + w); }};
eq(ids(sortSessions(S)), 'abc', 'overhead: all causes, biggest first (a=6, b=3, c=0)');
sessionFilters.cause = 'idle_gap';
eq(ids(sortSessions(S)), 'bac', 'overhead sorts by the selected cause (b=3, a=1)');
eq(ids(applySessionFilters(S)), 'ab', 'cause filter keeps only sessions with that cause');
sessionFilters.cause = 'uncached_input'; eq(ids(applySessionFilters(S)), '', 'no session has this cause');
sessionFilters.cause = 'idle_gap'; activeTab = 'waste'; eq(ids(applySessionFilters(S)), 'abc', 'the cause filter must not narrow the Waste scope'); activeTab = 'sessions';
sessionFilters.cause = 'uncached_input';
idx = null; eq(ids(applySessionFilters(S)), 'abc', 'findings not loaded yet: do not hide every row');
sessionSortCol = 'cache_share'; sessionSortDir = 'asc'; eq(ids(sortSessions(S)), 'bca', 'lowest cache share first');
console.log('ok');
""")
        self.assertEqual(out, "ok")

    def test_cause_in_the_url_belongs_to_the_active_screen(self):
        src = _slice("function applyURLState", "function restoreFromURL")
        out = _node(f"""
import {{ readSessionState, readWasteState }} from '{DRILL_URL}';
globalThis.window = {{ location: {{ search: '?cause=idle_gap&week=2026-11-02' }} }};
let seen = null, wasteCause = 'stale', wasteWeek = 'stale', wasteShowAll = true;
const setSessionControls = f => {{ seen = f; }}, readSessionFilters = () => {{}};
{src}
const eq = (g, w, m) => {{ if (g !== w) throw new Error(m + ': got ' + g + ', want ' + w); }};
applyURLState('waste'); eq(seen.cause, '', 'waste: not a Sessions filter'); eq(wasteCause, 'idle_gap', 'waste cause'); eq(wasteWeek, '2026-11-02', 'week');
applyURLState('sessions'); eq(seen.cause, 'idle_gap', 'sessions filter'); eq(wasteCause, null, 'sessions: no waste cause'); eq(wasteWeek, null, 'no week');
console.log('ok');
""")
        self.assertEqual(out, "ok")


@unittest.skipUnless(NODE, "node not installed")
class TestNavigationRestore(unittest.TestCase):
    def test_back_restores_range_models_timezone_and_drops_pending_trace(self):
        src = _slice("function restoreFromURL", "window.addEventListener('popstate'")
        out = _node(f"""
let calls = [], traceOpenSeq = 5, selectedRange = '30d', displayTZ = 'local', rawData = {{ all_models: ['m1', 'm2'] }}, activeTab = 'overview';
let urlTZ = 'utc';
globalThis.document = {{ querySelectorAll: () => [] }};
const markRange = () => {{}};
const readURLRange = () => '7d', readURLModels = all => new Set(['m1']), readURLTZ = () => urlTZ;
const buildFilterUI = (all, keep) => calls.push('models:' + [...keep]), renderBuildInfo = () => {{}};
const readURLTab = () => ({{ tab: 'sessions', hash: '' }}), applyURLState = () => {{}}, applyTabVisibility = () => {{}}, updateSortIcons = () => {{}};
const readURLSession = () => '', applyFilter = () => calls.push('filter'), loadData = () => calls.push('load'), gotoSection = () => {{}};
const readTraceLink = () => ({{}}), openSessionDetail = () => {{}};
globalThis.window = {{ location: {{ search: '' }} }};
{src}
const eq = (g, w, m) => {{ if (String(g) !== String(w)) throw new Error(m + ': got ' + g + ', want ' + w); }};
restoreFromURL();
eq(traceOpenSeq, 6, 'a pending trace open is invalidated'); eq(selectedRange, '7d', 'range'); eq(displayTZ, 'utc', 'tz');
eq(calls.join(), 'models:m1,load', 'models rebuilt, and a new zone refetches instead of just refiltering');
calls = []; restoreFromURL(); eq(calls.join(), 'models:m1,filter', 'same zone: refilter only');
console.log('ok');
""")
        self.assertEqual(out, "ok")

    def test_findings_panel_never_shows_another_sessions_buttons_or_a_stale_reload(self):
        src = _slice("const sessionFindings = new Map();", "// ── Trace: request bands")
        out = _node(f"""
const box = {{ dataset: {{}}, innerHTML: '', hidden: true, classList: {{ add() {{ box.hidden = true; }}, remove() {{ box.hidden = false; }} }} }};
globalThis.document = {{ getElementById: () => box }};
let selectedSessionDetail = null, pending = [];
const fetchSessionWaste = id => new Promise(res => pending.push({{ id, res }}));
const sessionFindingsHTML = p => 'HTML:' + p.tag, fillVerifySlots = () => {{}};
{src}
const eq = (g, w, m) => {{ if (g !== w) throw new Error(m + ': got ' + g + ', want ' + w); }};
const open = id => {{ selectedSessionDetail = {{ session: {{ session_id: id, project: 'p' }} }}; return renderSessionFindings(selectedSessionDetail); }};
(async () => {{
  let a = open('A'); pending[0].res({{ tag: 'A1' }}); await a; eq(box.innerHTML, 'HTML:A1', 'A drawn');
  let b = open('B'); eq(box.innerHTML, '', 'B opened: A is gone at once'); eq(box.hidden, true, 'hidden while B loads');
  pending[1].res({{ tag: 'B1' }}); await b; eq(box.innerHTML, 'HTML:B1', 'B drawn');
  // a reload clears the cache; the older request for the same session then resolves after the newer one
  const c1 = open('C'); sessionFindings.clear(); const c2 = open('C');
  pending[3].res({{ tag: 'new' }}); await c2; eq(box.innerHTML, 'HTML:new', 'newer drawn');
  pending[2].res({{ tag: 'old' }}); await c1; eq(box.innerHTML, 'HTML:new', 'an older request must not overwrite newer findings');
  console.log('ok');
}})();
""")
        self.assertEqual(out, "ok")


class TestWiring(unittest.TestCase):
    def test_markup_and_listeners(self):
        for needle in ('id="where-to-act"', 'id="session-findings"', 'id="session-cause-filter"',
                       "setSessionSort('overhead')", 'id="sessions-sort-note"'):
            self.assertIn(needle, HTML)
        self.assertIn("window.addEventListener('popstate', restoreFromURL)", APP)
        self.assertIn("closest('a[data-drill]')", APP)
        self.assertIn("history[push ? 'pushState' : 'replaceState']", APP)
        self.assertIn("updateURL(hash, changed)", _slice("function setTab(", "\n}"))  # a screen change is a history entry

    def test_url_carries_sessions_and_waste_state(self):
        url = _slice("function updateURL", "function readURLSession")
        self.assertIn("writeSessionState(", url)
        self.assertIn("writeWasteState(params, wasteCause, wasteWeek)", url)
        for fn in ("setWasteCause", "setWasteCell", "clearWasteWeek", "setSessionSort", "onSessionFilterChange", "clearSessionFilters"):
            body = _slice(f"function {fn}" if fn != "setWasteCause" and fn != "setWasteCell" else f"async function {fn}", "\n}\n")
            self.assertIn("updateURL()", body, fn)

    def test_waste_causes_link_to_their_top_sessions(self):
        self.assertIn('a data-drill href="${esc(sessionsByCauseHref(c.cause))}"', APP)
        self.assertIn("{ col: 'overhead', dir: 'desc' }", APP)

    def test_trace_opens_with_findings(self):
        body = _slice("function renderSessionDetail", "// One request per session per data load")
        self.assertIn("renderSessionFindings(d)", body)
        self.assertIn("fetchSessionWaste(id)", APP)
        self.assertIn("session_id=", (ROOT / "static" / "modules" / "api.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
