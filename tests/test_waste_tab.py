"""Waste tab contract. Evidence tiers, measurement status per cause x source, one
pricing basis, and the /api/waste payload the tab renders. Fixtures come from test_waste."""

import shutil
import sqlite3
import unittest
from pathlib import Path

import api
import pricing
import waste
from tests.test_waste import CAUSES, EXPECTED_TOP, FIXTURES, _Scanned

TIERS = {"observation", "attributed cause", "demonstrated saving"}


class TestAdvice(_Scanned):
    def _all(self):
        return [f for sid in FIXTURES for f in waste.report(self.db, session_id=sid)]

    def test_every_finding_names_its_tier_in_the_advice(self):
        for f in self._all():
            self.assertIn(f["tier"], TIERS)
            self.assertTrue(f["advice"].lower().startswith(f["tier"]), f["advice"])
            self.assertIn(f["fix"], f["advice"])

    def test_no_cause_claims_a_demonstrated_saving(self):
        # v1.1 (decision 4): nothing is measured before/after yet
        self.assertNotIn("demonstrated saving", {f["tier"] for f in self._all()})

    def test_evidence_is_turns_not_inputs(self):
        for f in self._all():
            self.assertTrue(f["evidence"], f["cause"])
            self.assertLessEqual(len(f["evidence"]), 5)
            for e in f["evidence"]:
                self.assertEqual(set(e), {"at", "tokens", "usd", "tool", "message_id", "agent_id"})
                self.assertNotIn("/", str(e["tool"] or ""))

    def test_session_usd_is_the_list_price_total(self):
        for f in self._all():
            self.assertAlmostEqual(f["session_usd"], self._session_cost(f["session_id"]), places=9)
            self.assertLessEqual(f["usd"], f["session_usd"] + 1e-9)

    def test_repeated_read_detail_names_the_tool(self):
        self.assertIn("Read", waste.report(self.db, session_id="reread")[0]["detail"])


class TestMeasurement(_Scanned):
    def test_claude_is_measured_for_every_cause(self):
        m = waste.measurement(self.db)
        self.assertEqual(m["sources"], ["claude"])
        for cause in CAUSES:
            self.assertEqual(m["causes"][cause]["claude"]["status"], "measured")

    def test_unmeasurable_source_is_shown_with_a_reason(self):
        copy = Path(self.tmp) / "mixed.db"
        shutil.copy(self.db, copy)
        conn = sqlite3.connect(copy)
        conn.execute("UPDATE turns SET agent='codex' WHERE session_id='clean'")
        conn.commit()
        conn.close()
        m = waste.measurement(str(copy))
        self.assertEqual(m["sources"], ["claude", "codex"])
        for cause in CAUSES:
            cell = m["causes"][cause]["codex"]
            self.assertEqual(cell["status"], "inferred" if cause == "resident_output" else "not measurable", cause)
            self.assertTrue(cell["reason"].strip(), cause)
            self.assertEqual(m["causes"][cause]["claude"]["status"], "measured")


    def test_unmeasurable_source_attributes_no_dollars(self):
        copy = Path(self.tmp) / "coord.db"
        shutil.copy(self.db, copy)
        conn = sqlite3.connect(copy)
        conn.execute("UPDATE turns SET agent='coordinator' WHERE session_id='idle'")
        conn.commit()
        conn.close()
        self.assertEqual(waste.report(str(copy), session_id="idle"), [])


class TestApi(_Scanned):
    def test_payload(self):
        p = api.get_waste(db_path=self.db)
        self.assertEqual(p["basis"]["pricing"], "API list rate")
        self.assertIn("lower bound", p["basis"]["label"])
        self.assertEqual({r["cause"] for r in p["rollup"]}, set(EXPECTED_TOP.values()) | {"idle_gap"})
        self.assertAlmostEqual(p["total_usd"], sum(f["usd"] for f in p["findings"]))
        self.assertNotIn("clean", {f["session_id"] for f in p["findings"]})
        self.assertIn("causes", p["measurement"])

    def test_rollup_matches_findings(self):
        p = api.get_waste(db_path=self.db)
        for r in p["rollup"]:
            mine = [f for f in p["findings"] if f["cause"] == r["cause"]]
            self.assertAlmostEqual(r["usd"], sum(f["usd"] for f in mine))
            self.assertEqual(r["sessions"], len(mine))

    def test_no_raw_path_leaves_the_api(self):
        import json
        self.assertNotIn("/src/", json.dumps(api.get_waste(db_path=self.db)))


class TestUi(unittest.TestCase):
    def test_waste_tab_is_wired(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        js = Path("static/modules/app.js").read_text(encoding="utf-8")
        self.assertIn('data-tab="waste"', html)
        self.assertIn('id="panel-waste"', html)
        self.assertIn("'waste'", js)
        self.assertIn("/api/waste", Path("static/modules/api.js").read_text(encoding="utf-8"))
        fmt = Path("static/modules/format.js").read_text(encoding="utf-8")
        self.assertIn("&quot;", fmt)  # esc() must be attribute-safe (data-session="...")
        # one basis: the tab states it and does not reprice with browser overrides
        self.assertIn("Priced at API list rate. Amounts are attributed", html)


if __name__ == "__main__":
    unittest.main()


class TestEvidenceIds(_Scanned):
    def test_evidence_message_ids_round_trip_to_the_trace(self):
        p = api.get_waste(db_path=self.db)
        checked = 0
        for f in p["findings"]:
            trace = {t["message_id"] for t in api.get_session_detail(f["session_id"], db_path=Path(self.db))["turns"]}
            for e in f["evidence"]:
                if e["message_id"]:
                    self.assertIn(e["message_id"], trace, f["cause"])
                    checked += 1
        self.assertGreater(checked, 0)


_HARNESS = """
%(SRC)s
const esc = s => String(s), fmt = n => String(n), fmtCostBig = c => '$' + c.toFixed(2);
const eq = (got, want, what) => { if (got !== want) throw new Error(what + ': got ' + got + ', want ' + want); };
const has = (h, n, what) => { if (!h.includes(n)) throw new Error(what + ': missing ' + n + ' in ' + h); };
const lacks = (h, n, what) => { if (h.includes(n)) throw new Error(what + ': unexpected ' + n); };
const recordButton = (p, c, n) => '<button class="wg-record" data-rproject="' + p + '" data-rnote="' + n + '">I made this change</button>';
const base = { id: 's1', detail: 'd', evidence: [{ message_id: 'm1', at: '2026-11-01T10:00:00Z', tool: 'Bash', tokens: 9, usd: 1 }], action: null };
// S1: rule, ranked calls with a Request link, estimate line with its basis
const a1 = { id: 'read_ranges', title: 'Read whole files 2 times', rule: 'RULE TEXT', est_usd: 1.5, est_basis: 'Estimate, lower bound: x',
  calls: [{ message_id: 'm1', at: 't', tool: 'Read', file: 'big.py', result_tokens: 12000, prompt_seq: 7 }] };
const s1 = nextStepCard({ ...base, cause: 'resident_output', action: a1 }, 'fix', 'why', true);
has(s1, '<details open>', 'S1 open for the top session'); has(s1, 'RULE TEXT', 'S1 rule'); has(s1, 'big.py', 'S1 file');
has(s1, 'Request 7', 'S1 request link'); has(s1, 'data-turn="m1"', 'S1 trace link'); has(s1, 'Estimate: about $1.50', 'S1 estimate'); has(s1, 'lower bound', 'S1 tier');
lacks(nextStepCard({ ...base, cause: 'resident_output', action: a1 }, 'f', 'w', false), '<details open>', 'S1 closed for other sessions');
const unk = nextStepCard({ ...base, cause: 'idle_gap', action: { id: 'subagent_waits', title: 'T', rule: 'R', est_usd: 1, est_basis: 'b', calls: [{ message_id: 'm4', at: 't', minutes: 8, tokens: 3, usd: 1, side: true, agent_type: null }] } }, 'f', 'w', true);
has(unk, '>subagent<', 'unknown subagent type is not main thread'); lacks(unk, 'main thread', 'unknown subagent');
// idle gap and small-jobs cards use their own columns
const gap = nextStepCard({ ...base, cause: 'idle_gap', action: { id: 'subagent_waits', title: 'T', rule: 'R', est_usd: 1, est_basis: 'b',
  calls: [{ message_id: 'm2', at: 't', minutes: 8, tokens: 300, usd: 1.2, warm_usd: 0.1, side: true, agent_type: 'task-implementer' }] } }, 'f', 'w', true);
has(gap, 'task-implementer', 'gap who'); has(gap, 'data-ids="m2"', 'highlights the action calls, not the evidence'); has(gap, '8m', 'gap length'); has(gap, 'data-turn="m2"', 'gap link');
const sj = nextStepCard({ ...base, cause: 'subagent_preamble', action: { id: 'small_jobs_inline', title: 'T', rule: 'R', est_usd: 1, est_basis: 'b',
  calls: [{ message_id: 'm3', at: 't', agent_type: 'Explore', turns: 3, usd: 0.4 }] } }, 'f', 'w', true);
has(sj, 'Explore', 'small job type'); has(sj, '>3<', 'small job turns');
has(nextStepCard({ ...base, cause: 'resident_output', action: a1 }, 'f', 'w', true, 'proj'), 'data-rnote="Read whole files 2 times"', 'S1 record button carries the rule title');
lacks(s1, 'wg-record', 'no button without a project');
// S2: no action on resident_output / subagent_preamble: no rule, no table
for (const c of ['resident_output', 'subagent_preamble']) {
  const h = nextStepCard({ ...base, cause: c }, 'fix', 'why', true);
  has(h, 'No single call stands out. This is the cost of the work.', 'S2 ' + c); lacks(h, 'wg-rule', 'S2 rule'); lacks(h, '<table>', 'S2 table');
}
// S3: cache miss keeps the table, links each row, adds the checklist
const s3 = nextStepCard({ ...base, cause: 'cache_miss_rebuild' }, 'fix', 'why', true);
has(s3, '<table>', 'S3 table'); has(s3, 'data-turn="m1"', 'S3 row link'); has(s3, 'Did you switch model', 'S3 checklist'); has(s3, 'CLAUDE.md or settings', 'S3 checklist');
// other causes keep the evidence table without a checklist
lacks(nextStepCard({ ...base, cause: 'repeated_reads' }, 'f', 'w', true), 'Usual causes', 'no checklist elsewhere');
console.log('ok');
"""


class TestNextStepCard(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_states(self):
        import subprocess
        src = (Path(__file__).resolve().parent.parent / "static" / "modules" / "app.js").read_text(encoding="utf-8")
        fns = src[src.index("const CACHE_MISS_CHECKLIST"):src.index("// Group findings into what a user acts on")]
        out = subprocess.run(["node", "-e", _HARNESS % {"SRC": fns}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "ok")


_VERIFY = """
const esc = s => String(s), fmtCostBig = c => '$' + c.toFixed(2);
%(SRC)s
const has = (h, n, what) => { if (!h.includes(n)) throw new Error(what + ': missing ' + n + ' in ' + h); };
const lacks = (h, n, what) => { if (h.includes(n)) throw new Error(what + ': unexpected ' + n); };
const mkSide = n => ({ sessions: n, cause_usd: 1, usd: 10, pct: 0.1 });
const mk = (status, b, a, extra) => ({ fix: { id: 1, fix_date: '2026-11-10', note: 'Read whole files 12 times' }, before: mkSide(b), after: mkSide(a),
  status, caveat: 'CAVEAT', min_sessions: 10, ...extra });
// S4: recorded, short of the minimum on the after side
const s4 = describe(mk('insufficient data', 20, 3, { reason: 'needs at least 10' }));
has(s4, 'Recorded 2026-11-10', 'S4 date'); has(s4, 'Read whole files 12 times', 'S4 rule'); has(s4, '3 of 10 sessions since. Check back.', 'S4 count');
lacks(s4, 'before:', 'S4 hides the comparison');
// short only on the before side: not "n of 10 since"
const s4b = describe(mk('insufficient data', 4, 15, { reason: 'needs at least 10' }));
lacks(s4b, 'Check back', 'before-side shortfall is not S4'); has(s4b, 'needs at least 10', 'reason kept');
// S5 keeps the before/after text; no material change points at the next call
const s5 = describe(mk('no material change', 20, 12));
has(s5, 'before: 20 sessions', 'S5 before'); has(s5, 'after: 12 sessions', 'S5 after'); has(s5, 'try the next call on the list', 'S5 hint'); has(s5, 'CAVEAT', 'S5 caveat');
lacks(describe(mk('reduced', 20, 12, { saving_usd: 5, basis: 'b' })), 'next call', 'hint only on no change');
has(recordButton('p', 'c', 'n"x'), 'data-rnote="n"x"', 'button carries the note');
// the card's button posts the rule title and reads Recorded from the response
(async () => {
  let posted = null;
  globalThis.fetch = async (u, o) => { if (o) posted = JSON.parse(o.body); return { ok: true, json: async () => ({ results: [] }) }; };
  globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };
  const mkBtn = () => ({ dataset: { rproject: 'p', rcause: 'c', rnote: 'n' }, outerHTML: '', disabled: false });
  const ok = mkBtn();
  await recordFix(ok);
  if (!posted || posted.project !== 'p' || posted.cause !== 'c' || posted.note !== 'n') throw new Error('wrong body ' + JSON.stringify(posted));
  if (!ok.outerHTML.includes('Recorded')) throw new Error('button did not read Recorded');
  // a failed post re-enables the button with an error
  globalThis.fetch = async () => ({ ok: false });
  const bad = mkBtn();
  await recordFix(bad);
  if (bad.disabled || !bad.textContent.includes('try again')) throw new Error('failed post left the button dead');
  // a fix already stored on the server (fresh page load, nothing recorded this session) hides the matching button
  resetVerify();
  globalThis.fetch = async () => ({ json: async () => ({ results: [{ fix: { id: 1, fix_date: '2026-11-10', project: 'p', cause: 'c', note: 'n' },
    before: mkSide(20), after: mkSide(3), status: 'insufficient data', caveat: 'x', min_sessions: 10 }] }) });
  const stored = mkBtn(), fresh = { dataset: { rproject: 'p', rcause: 'c', rnote: 'new rule' }, outerHTML: 'keep' };
  globalThis.document.querySelectorAll = q => q === '.wg-record' ? [stored, fresh] : q === '.wg-verify' ? [{ dataset: { vproject: 'p', vcause: 'c' }, innerHTML: '' }] : [];
  await fillVerifySlots(globalThis.document);
  if (!stored.outerHTML.includes('Recorded')) throw new Error('stored fix left its button active');
  if (fresh.outerHTML !== 'keep') throw new Error('a different rule lost its button');
  console.log('ok');
})();
"""


class TestVerifyWording(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_s4_and_s5_wording(self):
        import re
        import subprocess
        src = (Path(__file__).resolve().parent.parent / "static" / "modules" / "verify.js").read_text(encoding="utf-8")
        src = re.sub(r"^import .*$", "", src, flags=re.M).replace("export ", "")
        out = subprocess.run(["node", "-e", _VERIFY % {"SRC": src}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "ok")
