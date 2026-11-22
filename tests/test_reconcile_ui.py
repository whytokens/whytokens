"""Reconcile panel logic under node: month switching and displayed-figure tie-out."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
URL = (Path(__file__).resolve().parent.parent / "static" / "modules" / "reconcile.js").as_uri()


def _node(body):
    out = subprocess.run([NODE, "--input-type=module", "-e", body], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "TZ": "UTC"})
    if out.returncode:
        raise AssertionError(out.stderr or out.stdout)
    return out.stdout


@unittest.skipUnless(NODE, "node not installed")
class TestReconcilePanel(unittest.TestCase):
    def test_loading_a_month_sets_or_clears_amount_and_reading_time_together(self):
        _node(f"""
import assert from 'node:assert/strict';
const esc = {{ createElement: () => {{ let t = ''; return {{ set textContent(v) {{ t = String(v); }}, get innerHTML() {{ return t.replace(/&/g, '&amp;').replace(/</g, '&lt;'); }} }}; }} }};
import {{ initReconcile }} from '{URL}';
const els = {{}}, hs = {{}};
for (const id of ['reconcile','rec-period','rec-asof','rec-billed','rec-save','rec-clear','rec-check','rec-out','rec-status','rec-session','rec-usage','rec-session-out'])
  els[id] = {{ value: '', textContent: '', innerHTML: '', disabled: false, open: false, addEventListener(t, f) {{ (hs[id] ??= {{}})[t] = f; }} }};
globalThis.document = {{ getElementById: id => els[id], querySelector: () => null, createElement: esc.createElement }};
const body = r => ({{ period: 'x', cut: 'c', basis: 'b', tz: 'utc', shown: 1, utc_total: 1, gap: null, rows: [], remainder: {{ label: 'r', usd: null }},
  info: {{ advisor_estimates: {{ usd: 0, turns: 0 }}, raised_output_turns: 0, coordinator_lower_bound: {{ usd: 0 }} }}, coverage: {{ earliest_claude: null, label: 'l', projects_first_seen_in_period: [] }}, ...r }});
let reply;
globalThis.fetch = async () => ({{ ok: true, status: 200, json: async () => body(reply) }});
const settle = () => new Promise(r => setTimeout(r, 5));
initReconcile(() => 'utc');
els['rec-period'].value = '2026-09';
reply = {{ billed: 100, as_of: '2026-09-15T12:00:00Z' }};
hs['rec-period'].change(); await settle();
assert.equal(els['rec-asof'].value, '2026-09-15T12:00:00');
els['rec-period'].value = '2026-10';
reply = {{ billed: null, as_of: null }};
hs['rec-period'].change(); await settle();
assert.equal(els['rec-asof'].value, '');
assert.equal(els['rec-billed'].value, '');
let posted;
globalThis.fetch = async (u, init) => {{ if (init) posted = JSON.parse(init.body); return {{ ok: true, status: 200, json: async () => body(reply) }}; }};
els['rec-period'].value = '2026-09';
reply = {{ billed: 100, as_of: '2026-09-15T12:00:00.100400Z' }};
hs['rec-period'].change(); await settle();
els['rec-billed'].value = '120';
hs['rec-save'].click(); await settle();
assert.equal(posted.as_of, '2026-09-15T12:00:00.100400Z');   // untouched reading time is resent exactly
console.log('ok');
""")

    def test_displayed_rows_add_up_at_cent_precision(self):
        _node(f"""
import assert from 'node:assert/strict';
const esc = {{ createElement: () => {{ let t = ''; return {{ set textContent(v) {{ t = String(v); }}, get innerHTML() {{ return t.replace(/&/g, '&amp;').replace(/</g, '&lt;'); }} }}; }} }};
globalThis.document = esc;
import {{ reconcileHTML }} from '{URL}';
const r = {{ period: 'p', cut: 'c', basis: 'b', tz: 'utc', shown: 0.0108, billed: 0, gap: 0.0108, as_of: null,
  rows: [{{ key: 'a', kind: 'measured', usd: 0.0054, turns: 1, label: 'Alpha' }}, {{ key: 'b', kind: 'scenario', usd: 0.0054, turns: 1, label: 'Beta' }}],
  remainder: {{ label: 'Unexplained', usd: 0 }},
  info: {{ advisor_estimates: {{ usd: 0, turns: 0 }}, raised_output_turns: 0, coordinator_lower_bound: {{ usd: 0 }} }}, coverage: {{ earliest_claude: null, label: 'l', projects_first_seen_in_period: [] }} }};
const html = reconcileHTML(r);
const v = l => Number(html.split('<tr').find(t => t.includes(l)).match(/\$(-?[\d.]+)/)[1]);
const gap = v('Gap'), a = v('Alpha'), b = v('Beta'), rem = v('Unexplained');
assert.ok(Math.abs(gap - (a + b + rem)) < 1e-9, JSON.stringify({{ gap, a, b, rem }}));
console.log('ok');
""")


if __name__ == "__main__":
    unittest.main()
