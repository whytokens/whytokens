"""provenance: one vocabulary served to the UI, observed vs inferred $ split per waste cause,
'not measurable' rendered as unknown (never zero), real values in the verify hint.
Run: python -m unittest discover -s tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import provenance  # noqa: E402
import waste  # noqa: E402
from scanner import get_db, init_db  # noqa: E402

JS = lambda name: (ROOT / "static" / "modules" / name).as_uri()  # noqa: E731


def node(module, expr):
    stub = ("globalThis.document={createElement:()=>({set textContent(v){this.innerHTML="
            "String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}})};")  # esc() needs a DOM
    code = stub + f"import * as a from {json.dumps(JS(module))}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _f(cause, tier, usd):
    return {"cause": cause, "tier": tier, "attribution": provenance.attribution(tier), "usd": usd}


class TestVocabulary(unittest.TestCase):
    def test_labels_are_the_agreed_five_with_tooltips(self):
        self.assertEqual(set(provenance.LABELS), {"official", "local_estimate", "unknown", "observed", "inferred"})
        self.assertTrue(all(len(v) > 20 for v in provenance.LABELS.values()))

    def test_every_waste_tier_maps_to_observed_or_inferred(self):
        self.assertEqual({provenance.attribution(t) for t in waste.TIERS.values()}, {"observed", "inferred"})
        self.assertEqual(provenance.attribution("deferred tool"), "observed")
        self.assertEqual(provenance.attribution("inferred"), "inferred")

    def test_served_over_the_api(self):
        self.assertIs(api.provenance.LABELS, provenance.LABELS)
        src = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn('"/api/provenance"', src)
        self.assertIn("self._send_json(provenance.LABELS)", src)


class TestRollupSplit(unittest.TestCase):
    def test_mixed_cause_keeps_observed_and_inferred_dollars_apart(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        c = get_db(Path(f.name)); init_db(c); c.close()
        findings = [_f("resident_output", "attributed cause", 859.0), _f("resident_output", "inferred", 55.0),
                    _f("deferred_tool_load", "inferred", 3.0)]
        with mock.patch.object(waste, "report", return_value=findings):
            roll = {r["cause"]: r for r in api.get_waste(f.name)["rollup"]}
        r = roll["resident_output"]
        self.assertEqual((r["observed_usd"], r["inferred_usd"], r["usd"]), (859.0, 55.0, 914.0))
        self.assertEqual((roll["deferred_tool_load"]["observed_usd"], roll["deferred_tool_load"]["inferred_usd"]), (0.0, 3.0))

    def test_report_findings_carry_attribution(self):
        src = (ROOT / "waste.py").read_text(encoding="utf-8")
        self.assertIn('"attribution": provenance.attribution(tier)', src)


class TestFrontend(unittest.TestCase):
    def test_split_sums_by_attribution(self):
        got = node("provenance.js", "(()=>{const c={observed:0,inferred:0};"
                   "a.addSplit(c,{attribution:'observed',usd:859});a.addSplit(c,{attribution:'inferred',usd:55});return c})()")
        self.assertEqual(got, {"observed": 859, "inferred": 55})

    def test_unknown_is_a_dash_never_zero(self):
        self.assertEqual(node("provenance.js", "a.orUnknown(0, true)"), "—")
        self.assertEqual(node("provenance.js", "a.orUnknown(0, false)"), "0")
        self.assertEqual(node("provenance.js", "a.orUnknown(null, false)"), "—")

    def test_badge_uses_served_tooltip_and_escapes(self):
        h = node("provenance.js", "(()=>{a.setProvenance({official:'t \"x\"'});return a.provBadge('official')})()")
        self.assertIn("prov-official", h)
        self.assertIn("t &quot;x&quot;", h)

    def test_verify_hint_is_prefilled(self):
        got = node("verify.js", "a.recordHint(\"it's $(x)\", 'resident_output')")
        self.assertIn("--project 'it''s $(x)' --cause resident_output", got)
        self.assertNotIn("…", got)

    def test_sortable_session_headers(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for key, kind in (("cost", "local_estimate"), ("cache_read", "official"), ("input", "official")):
            self.assertIn(f"""<th data-prov="{kind}" class="sortable" onclick="setSessionSort('{key}')">""", html)

    def test_every_cost_and_token_header_is_tagged(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for th in ("Est. Cost", "Cache Read", "Cache Creation"):
            self.assertRegex(html, rf'<th[^>]*data-prov="[a-z_]+"[^>]*>{th}')
        self.assertRegex(html, r'data-prov="local_estimate"[^>]*>Est\. Cost')
        self.assertRegex(html, r'data-prov="official"[^>]*>Cache Read')


if __name__ == "__main__":
    unittest.main()
