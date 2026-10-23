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
                self.assertEqual(set(e), {"at", "tokens", "usd", "tool"})
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
            self.assertEqual(cell["status"], "not measurable", cause)
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
