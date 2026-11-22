"""Budget band, Settings control and what-if lines: pure frontend logic under node.

The wording modules (budget.js, whatif.js) are imported directly; app.js is sliced like
tests/test_drilldown.py. scripts/falsify_budget.py breaks each piece and checks this file goes red.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "static" / "modules"
APP = (MODULES / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BUDGET_URL, WHATIF_URL, DRILL_URL = ((MODULES / f).as_uri() for f in ("budget.js", "whatif.js", "drill.js"))
NODE = shutil.which("node")

LINKS = "{ sessions: '?range=month&tab=sessions&sort=cost&dir=desc', waste: '?range=month&tab=waste', settings: '?tab=settings' }"
OVER = "{ month: '2026-11', spent: 55, budget: 150, projected: 195, gap: 45, needed_daily: 4.75, method: 'trailing_7d', days_remaining: 20 }"


def _node(body):
    out = subprocess.run([NODE, "--input-type=module", "-e", body], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _slice(start, end):
    a = APP.index(start)
    return APP[a:APP.index(end, a + 1)]


_HAS = """
globalThis.document = { createElement: () => ({ set textContent(v) { this.t = v; }, get innerHTML() { return this.t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }) };
const has = (h, n, w) => { if (!h.includes(n)) throw new Error(w + ': missing ' + n + ' in ' + h); };
const lacks = (h, n, w) => { if (h.includes(n)) throw new Error(w + ': unexpected ' + n); };
"""


@unittest.skipUnless(NODE, "node not installed")
class TestBandWording(unittest.TestCase):
    def run_js(self, check):
        return _node(f"import {{ budgetBandHTML }} from '{BUDGET_URL}';\n{_HAS}\nconst L = {LINKS};\n{check}\nconsole.log('ok');")

    def test_over_budget_says_so_in_words_with_the_overshoot(self):
        self.assertEqual(self.run_js(f"""
const h = budgetBandHTML({OVER}, L);
has(h, 'Projected to exceed the budget by $45.00.', 'verdict'); has(h, 'Over by', 'cell'); has(h, 'budget-over', 'style hook');
has(h, '$55.00', 'spent'); has(h, '$150.00', 'budget'); has(h, '$195.00', 'projected'); has(h, '$4.75/day', 'needed rate');
has(h, 'estimated at API list price', 'basis');
has(h, 'Projection = spent + trailing 7-day daily average x days remaining', 'method');
has(h, 'data-drill href="?range=month&amp;tab=sessions&amp;sort=cost&amp;dir=desc"', 'sessions link, this month by cost');
has(h, 'tab=waste', 'a gap links to Waste'); lacks(h, 'Set a budget', 'a budget is set');
"""), "ok")

    def test_under_budget_has_no_waste_link_and_no_overshoot(self):
        self.assertEqual(self.run_js("""
const h = budgetBandHTML({ month: '2026-11', spent: 55, budget: 300, projected: 195, gap: -105, needed_daily: 12.25, method: 'trailing_7d', days_remaining: 20 }, L);
has(h, 'Projected to finish $105.00 under the budget.', 'verdict'); lacks(h, 'exceed', 'not over'); lacks(h, 'tab=waste', 'no gap, no waste link');
lacks(h, 'budget-over', 'not over');
"""), "ok")

    def test_no_budget_shows_spend_and_projection_and_a_way_to_set_one(self):
        self.assertEqual(self.run_js("""
const h = budgetBandHTML({ month: '2026-11', spent: 55, budget: null, projected: 195, gap: null, needed_daily: null, method: 'trailing_7d', days_remaining: 20 }, L);
has(h, '$55.00', 'spent'); has(h, '$195.00', 'projection'); has(h, 'Not set', 'budget cell');
has(h, 'data-drill href="?tab=settings"', 'set-a-budget link'); has(h, 'Set a budget', 'set-a-budget text');
lacks(h, '$0.00', 'an unset budget is never shown as zero'); lacks(h, 'exceed', 'no verdict'); lacks(h, 'Needed daily', 'no needed rate'); lacks(h, 'under the budget', 'no verdict');
"""), "ok")

    def test_early_month_names_its_method(self):
        self.assertEqual(self.run_js("""
const h = budgetBandHTML({ month: '2026-11', spent: 6, budget: 100, projected: 60, gap: -40, needed_daily: 3.5, method: 'month_to_date', days_remaining: 27 }, L);
has(h, 'month-to-date average: fewer than 7 days of data', 'method'); lacks(h, 'trailing 7-day', 'not the trailing text');
"""), "ok")

    def test_edges(self):
        self.assertEqual(self.run_js("""
const done = budgetBandHTML({ month: '2026-11', spent: 160, budget: 150, projected: 200, gap: 50, needed_daily: -1, method: 'trailing_7d', days_remaining: 10 }, L);
has(done, 'Budget already spent', 'no negative rate'); lacks(done, '-$', 'no negative dollars');
const last = budgetBandHTML({ month: '2026-11', spent: 55, budget: 150, projected: 55, gap: -95, needed_daily: null, method: 'trailing_7d', days_remaining: 0 }, L);
has(last, 'Needed daily rate', 'cell'); has(last, '—', 'no days left: a dash, not a number');
if (budgetBandHTML(null, L) !== '' || budgetBandHTML({ error: 'x' }, L) !== '') throw new Error('unavailable must draw nothing');
"""), "ok")


@unittest.skipUnless(NODE, "node not installed")
class TestWhatifWording(unittest.TestCase):
    def run_js(self, check):
        return _node(f"import {{ whatifCellHTML, lanesLineHTML, WHATIF_WORDING }} from '{WHATIF_URL}';\n{_HAS}\n{check}\nconsole.log('ok');")

    def test_cell_shows_delta_or_a_dash(self):
        self.assertEqual(self.run_js("""
const c = whatifCellHTML({ model: 'm', to: 'claude-sonnet-5-5', actual_usd: 20, repriced_usd: 10, delta_usd: -10 });
has(c, '−$10.00', 'delta'); has(c, 'claude-sonnet-5-5', 'target'); has(c, 'Repricing only. It does not say the cheaper model would have done the work.', 'tooltip wording');
for (const none of [undefined, { model: 'h', to: null, delta_usd: null }]) {
  const d = whatifCellHTML(none); has(d, '>—<', 'dash'); lacks(d, '$', 'no figure when there is no cheaper tier');
}
"""), "ok")

    def test_lane_line_is_visible_text_with_the_wording(self):
        self.assertEqual(self.run_js("""
const res = { actual_usd: 4, repriced_usd: 2, delta_usd: -2, by_model: [{ to: 'claude-sonnet-5-5' }, { to: 'claude-sonnet-5-5' }, { to: null }] };
const h = lanesLineHTML(res);
has(h, 'These lanes repriced on claude-sonnet-5-5: $2.00 (was $4.00).', 'line'); has(h, WHATIF_WORDING, 'sentence next to the figure');
lacks(h, 'title=', 'the sentence is not hidden in a tooltip');
for (const none of [null, { error: 'x' }, { by_model: [] }, { by_model: [{ to: null }] }]) if (lanesLineHTML(none) !== '') throw new Error('nothing to reprice must draw nothing');
"""), "ok")


@unittest.skipUnless(NODE, "node not installed")
class TestAppLogic(unittest.TestCase):
    def test_band_drops_a_stale_response_and_links_this_month(self):
        src = _slice("let budgetGen = 0;", "async function renderBudgetSetting")
        out = _node(f"""
import {{ drillHref }} from '{DRILL_URL}';
{_HAS}
let displayTZ = 'utc', activeTab = 'overview', pending = [];
const store = {{ get: () => null }};
const fetchBudget = () => new Promise(res => pending.push(res));
const budgetBandHTML = (m, links) => JSON.stringify({{ tag: m && m.tag, links }});
const slot = {{ innerHTML: '' }}; globalThis.document = {{ getElementById: () => slot }};
{src}
const eq = (g, w, m) => {{ if (g !== w) throw new Error(m + ': got ' + g + ', want ' + w); }};
(async () => {{
  const a = renderBudgetBand(), b = renderBudgetBand();
  pending[1]({{ tag: 'new' }}); await b; pending[0]({{ tag: 'old' }}); await a;
  const got = JSON.parse(slot.innerHTML);
  eq(got.tag, 'new', 'an older response must not overwrite a newer one');
  has(got.links.sessions, 'range=month', 'this month, whatever the range filter'); has(got.links.sessions, 'tab=sessions', 'sessions');
  has(got.links.sessions, 'sort=cost&dir=desc', 'by cost'); has(got.links.settings, 'tab=settings', 'settings');
  has(got.links.waste, 'tab=waste', 'waste');
  slot.innerHTML = ''; activeTab = 'sessions'; const c = renderBudgetBand(); pending[2]({{ tag: 'x' }}); await c; eq(slot.innerHTML, '', 'only the Overview shows the band');
  slot.innerHTML = 'stale'; activeTab = 'overview'; budgetShown = false; await renderBudgetBand(); eq(slot.innerHTML, '', 'the Settings checkbox hides the band'); eq(pending.length, 3, 'a hidden band fetches nothing'); budgetShown = null;
  activeTab = 'overview'; const d = renderBudgetBand(); pending[3] && 0;
  console.log('ok');
}})();
""")
        self.assertEqual(out, "ok")
        self.assertIn("sessionLanes.clear()", _slice("async function loadData", "let autoRefreshTimer"))  # a rescan drops cached lanes

    def test_band_session_link_carries_the_zone_it_was_computed_in(self):
        src = _slice("let budgetGen = 0;", "async function renderBudgetSetting")
        for tz in ("local", "utc"):
            out = _node(f"""
import {{ drillHref }} from '{DRILL_URL}';
let displayTZ = '{tz}', activeTab = 'overview';
const store = {{ get: () => null }};
const fetchBudget = async () => ({{}});
const budgetBandHTML = (m, links) => links.sessions;
const slot = {{ innerHTML: '' }}; globalThis.document = {{ getElementById: () => slot }};
{src}
await renderBudgetBand(); console.log(slot.innerHTML);
""")
            self.assertIn(f"tz={tz}", out)

    def test_save_validates_before_posting_and_clear_sends_null(self):
        src = _slice("async function saveOrClearBudget", "// Model what-if")
        out = _node(f"""
let sent = [], input = {{ value: '' }}, status = {{ textContent: '' }};
const postBudget = async v => {{ sent.push(v); }};
globalThis.document = {{ getElementById: id => id === 'budget-input' ? input : status }};
{src}
(async () => {{
  const eq = (g, w, m) => {{ if (JSON.stringify(g) !== JSON.stringify(w)) throw new Error(m + ': got ' + JSON.stringify(g) + ', want ' + JSON.stringify(w)); }};
  for (const bad of ['', 'abc', '0', '-5', 'Infinity']) {{ input.value = bad; await saveBudget(); }}
  eq(sent, [], 'invalid input never reaches the server');
  input.value = ' 250.5 '; await saveBudget(); eq(sent, [250.5], 'valid amount');
  await clearBudget(); eq(sent, [250.5, null], 'clear sends null'); eq(input.value, '', 'input emptied');
  console.log('ok');
}})();
""")
        self.assertEqual(out, "ok")
        self.assertIn("sessionLanes.clear()", _slice("async function loadData", "let autoRefreshTimer"))  # a rescan drops cached lanes

    def test_model_table_row_carries_the_what_if_cell_from_the_server_rows(self):
        src = _slice("function renderModelCostTable", "// ── Project cost table sorting")
        out = _node(f"""
{_HAS}
const sortModels = x => x, modelLimit = 9, esc = s => String(s), fmt = n => String(n), orUnknown = v => v, isEstimated = () => false;
const costCellHTML = (m, c) => '<td>cost</td>', renderTableToggle = () => {{}};
const modelWhatif = new Map([['opus', {{ model: 'opus', to: 'sonnet', actual_usd: 20, repriced_usd: 10, delta_usd: -10 }}]]);
const whatifCellHTML = r => '<td>WHATIF:' + (r ? r.to : 'none') + '</td>';
let html = ''; globalThis.document = {{ getElementById: () => ({{ set innerHTML(v) {{ html = v; }} }}) }};
{src}
renderModelCostTable([{{ model: 'opus', turns: 1, input: 1, output: 1, cache_read: 1, cache_creation: 1, cost: 1 }}, {{ model: 'haiku', turns: 1, input: 1, output: 1, cache_read: 1, cache_creation: 1, cost: 1 }}]);
has(html, 'WHATIF:sonnet', 'row with a cheaper tier'); has(html, 'WHATIF:none', 'row the server left out');
console.log('ok');
""")
        self.assertEqual(out, "ok")
        self.assertIn("sessionLanes.clear()", _slice("async function loadData", "let autoRefreshTimer"))  # a rescan drops cached lanes

    def test_an_older_expand_response_leaves_a_newer_expand_alone(self):
        src = _slice("const expandGen = new Map();", "function renderToolCostTable")
        out = _node(f"""
globalThis.CSS = {{ escape: s => s }};
let rows = [], click, pending = [];
const mk = html => {{ const r = {{ html, live: true, remove() {{ r.live = false; rows = rows.filter(x => x !== r); }},
  insertAdjacentHTML(_, h) {{ if (r.live && h) rows.push(mk(h)); }} }}; return r; }};
const row = {{ isConnected: true, dataset: {{}}, insertAdjacentHTML(_, h) {{ if (h) rows.push(mk(h)); }} }};
const attrs = {{ 'aria-expanded': 'false' }};
const btn = {{ dataset: {{ breakdown: 's1' }}, innerHTML: '', closest: q => q === 'tr' ? row : btn, getAttribute: k => attrs[k], setAttribute: (k, v) => {{ attrs[k] = v; }} }};
const body = {{ addEventListener: (_, fn) => {{ click = fn; }},
  querySelectorAll: q => rows.filter(r => q.startsWith('tr.breakdown-subrow') ? r.html.includes('breakdown-subrow') : true),
  querySelector: () => rows.find(r => r.html.includes('session-totals-subrow')) || null }};
globalThis.document = {{ getElementById: () => body }};
const esc = s => String(s), sessionBreakdownCache = new Map(), wastePayload = null;
const fetchSessionBreakdown = () => new Promise(res => pending.push(res));
const sessionTotalsRowHTML = () => '<tr class="session-totals-subrow">', lanesWhatif = async () => '', llm = null;
const breakdownRowsHTML = (id, r) => '<tr class="breakdown-subrow">ACTORS ' + r[0];
{src}
wireSessionExpand();
const count = n => rows.filter(r => r.html.includes(n)).length;
(async () => {{
  const first = click({{ target: btn }});              // open: request 1 in flight
  await click({{ target: btn }});                      // collapse
  const second = click({{ target: btn }});             // reopen: request 2 in flight
  pending[1]({{ rows: ['new'] }}); await second;
  pending[0]({{ rows: ['old'] }}); await first;        // the older response lands last
  if (count('ACTORS new') !== 1 || count('ACTORS old') !== 0) throw new Error('stale response changed the open rows: ' + rows.map(r => r.html));
  if (count('session-totals-subrow') !== 1 || count('Loading') !== 0) throw new Error('leftover rows: ' + rows.map(r => r.html));
  await click({{ target: btn }}); await click({{ target: btn }});   // collapse, reopen: served from the cache
  if (count('ACTORS new') !== 1 || count('ACTORS old') !== 0) throw new Error('the stale response reached the cache: ' + rows.map(r => r.html));
  console.log('ok');
}})();
""")
        self.assertEqual(out, "ok")

    def test_lane_line_is_appended_to_the_breakdown_rows(self):
        src = _slice("function breakdownRowsHTML", "/** Per-model rows")
        out = _node(f"""
{_HAS}
const esc = s => String(s), fmt = n => String(n), fmtCost = c => '$' + c;
const summarizeBreakdown = () => [{{ label: 'Explore', kind: 'agent', cost: 1, turns: 1, input: 1, output: 1, cache_read: 1, cache_creation: 1, models: new Map([['m', 1]]), slices: [] }}];
const modelRowsHTML = () => '';
{src}
has(breakdownRowsHTML('s1', [1], 'd', 'LANE LINE'), 'whatif-row', 'extra row'); has(breakdownRowsHTML('s1', [1], 'd', 'LANE LINE'), 'LANE LINE', 'text');
lacks(breakdownRowsHTML('s1', [1], 'd'), 'whatif-row', 'no line without a figure');
const rows = breakdownRowsHTML('s1', [1], 'd');
has(rows, '<td class="branch-cell" colspan="9"><table class="sub-table">', 'actors sit in one detail row as wide as the list');
for (const h of ['Who', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost']) has(rows, '>' + h + '</th>', 'the detail table names its columns');
if (rows.split('data-parent-session').length !== 2) throw new Error('one removable detail row, not one per actor');
lacks(rows, 'colspan="12"', 'no cell wider than the nine-column list');
console.log('ok');
""")
        self.assertEqual(out, "ok")
        self.assertIn("sessionLanes.clear()", _slice("async function loadData", "let autoRefreshTimer"))  # a rescan drops cached lanes


class TestWiring(unittest.TestCase):
    def test_markup(self):
        for needle in ('id="budget-band"', 'id="budget-input"', "onclick=\"saveBudget()\"", "onclick=\"clearBudget()\"",
                       "Monthly budget (USD, estimated at API list price)", "If on next tier down", 'id="session-whatif"'):
            self.assertIn(needle, HTML)
        self.assertLess(HTML.index('id="budget-band"'), HTML.index('id="stats-row"'))
        self.assertLess(HTML.index('id="stats-row"'), HTML.index('id="where-to-act"'))  # untouched, still under the stats row

    def test_overview_and_settings_are_wired(self):
        ov = _slice("case 'overview':", "case 'sessions':")
        self.assertIn("renderBudgetBand();", ov)
        self.assertIn("loadModelWhatif();", ov)
        self.assertIn("renderBudgetSetting();", APP)
        self.assertIn("window.saveBudget = saveBudget", APP)
        self.assertIn("renderTraceWhatif(d);", _slice("function renderSessionDetail", "// One request per session per data load"))
        self.assertIn("breakdownRowsHTML(sessionId, payload.rows || [], day, whatifLine)", APP)
        self.assertIn("whatifCellHTML(modelWhatif.get(m.model))", APP)

    def test_prices_live_only_on_the_server(self):
        for name in ("budget.js", "whatif.js"):
            src = (MODULES / name).read_text(encoding="utf-8")
            self.assertNotIn("PRICING", src, name)
            self.assertNotIn("fetchPricing", src, name)
            self.assertIsNone(re.search(r"\d\.\d\d\s*/\s*MTok|per_mtok|\*\s*1e6", src, re.I), name)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", src), name)  # tokens only
        css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        block = css[css.index("/* Overview budget band"):]
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block))

    def test_api_client_sends_the_token_and_the_null_clear(self):
        api_js = (MODULES / "api.js").read_text(encoding="utf-8")
        body = api_js[api_js.index("export async function postBudget"):]
        self.assertIn("'X-Whytokens-Token'", body)
        self.assertIn("JSON.stringify({ monthly_usd: monthlyUsd })", body)



class TestStatCardsFillWholeRows(unittest.TestCase):
    def test_every_column_count_divides_the_number_of_cards(self):
        import re
        root = Path(__file__).resolve().parent.parent / "static"
        css = (root / "styles.css").read_text(encoding="utf-8")
        js = (root / "modules" / "app.js").read_text(encoding="utf-8")
        cards = js[js.index("const stats = ["):js.index("document.getElementById('stats-row').innerHTML")].count("{ label: ")
        cols = [int(n) for n in re.findall(r"\.stats-row \{[^}]*?repeat\((\d+),", css)]
        self.assertEqual(cards, 8)
        self.assertEqual(sorted(cols), [2, 4, 8])  # phone, normal, wide
        for n in cols:
            self.assertEqual(cards % n, 0, n)  # no card left alone on a last row
        self.assertNotRegex(css, r"\.stats-row \{[^}]*auto-fit")  # auto-fit put 7 in a row and 1 below


if __name__ == "__main__":
    unittest.main()


class TestWhatifRequestGuards(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_older_failures_and_rescans_do_not_leave_stale_what_if_figures(self):
        from tests.test_drilldown import _node, _slice
        src = _slice("let modelWhatif = new Map()", "async function renderTraceWhatif")
        out = _node(f"""
let activeTab = 'x', displayTZ = 'local', lastByModel = [], selectedRange = '30d', calls = [];
const getRangeBounds = () => ({{ start: 'a', end: 'b' }}), renderModelCostTable = () => {{}}, lanesLineHTML = r => 'LINE:' + r.v;
let pend = [];
const fetchWhatif = p => new Promise((res, rej) => pend.push({{ p, res, rej }}));
{src}
const eq = (g, w, m) => {{ if (g !== w) throw new Error(m + ': got ' + g + ', want ' + w); }};
(async () => {{
  const a = loadModelWhatif(), b = loadModelWhatif();   // two overlapping loads
  eq(pend[0].p.tz, 'local', 'the display zone is sent');
  pend[1].res({{ by_model: [{{ model: 'm', delta_usd: -1 }}] }}); await b;
  pend[0].rej(new Error('boom')); await a;
  eq(modelWhatif.size, 1, 'an older failure must not blank the newer result');
  // lanes: a failed older request must not delete a newer cache entry; a reload clears the cache
  const l1 = lanesWhatif('s1'); sessionLanes.clear(); const l2 = lanesWhatif('s1');
  pend[2].rej(new Error('x')); await l1;
  eq(sessionLanes.get('s1'), l2, 'the newer entry survives an older failure');
  pend[3].res({{ v: 1 }}); eq(await l2, 'LINE:1', 'lane line');
  console.log('ok');
}})();
""")
        self.assertEqual(out, "ok")
        self.assertIn("sessionLanes.clear()", _slice("async function loadData", "let autoRefreshTimer"))  # a rescan drops cached lanes
