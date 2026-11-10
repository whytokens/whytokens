"""cache_miss_rebuild: unexpected prefix misses are waste; expected rebuilds are reported, not waste."""
import shutil
import tempfile
import unittest
from pathlib import Path

import waste
from scanner import scan
from tests.test_waste import MODEL, _rec, _ts

OTHER = "claude-opus-4-5"


def _session(sid, turns):
    """turns: (minute, read, create, kw). A 20k prefix is written first, then each turn as given."""
    lines = [_rec(sid, 1, _ts(0), read=0, create=20_000)]
    for n, (minute, read, create, kw) in enumerate(turns, 2):
        line = _rec(sid, n, _ts(minute), read=read, create=create, compact=kw.get("compact", False))
        lines.append(line.replace(MODEL, kw["model"]) if kw.get("model") else line)
    return lines


FIXTURES = {
    # known good: warm reads all the way
    "warm": _session("warm", [(m, 20_000 + 400 * i, 400, {}) for i, m in enumerate((3, 6, 9, 12))]),
    # full miss 3 min after the last turn, same model, no compaction: the prefix changed
    "miss": _session("miss", [(3, 20_000, 400, {}), (6, 0, 20_400, {}), (9, 20_800, 400, {})]),
    # miss after 10 min: past the 5m TTL, so idle_gap owns it, not a second claim
    "ttl": _session("ttl", [(3, 20_000, 400, {}), (13, 0, 20_400, {}), (16, 20_800, 400, {})]),
    "compact": _session("compact", [(3, 20_000, 400, {"compact": True}), (6, 0, 20_400, {})]),
    "switch": _session("switch", [(3, 20_000, 400, {}), (6, 0, 20_400, {"model": OTHER})]),
    # 45 min gap: idle_gap owns it, so no second claim
    "idle": _session("idle", [(3, 20_000, 400, {}), (48, 0, 20_400, {})]),
}


class TestCacheMissRebuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        scan(projects_dir=proj.parent, db_path=cls.db, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, sid):
        rebuilds = []
        found = waste.report(self.db, sid, min_share=0, rebuilds=rebuilds)
        return {f["cause"]: f for f in found}, rebuilds

    def test_known_good_is_silent(self):
        causes, rebuilds = self._run("warm")
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual(rebuilds, [])

    def test_unexpected_miss_is_waste_with_a_likely_cause(self):
        causes, rebuilds = self._run("miss")
        f = causes["cache_miss_rebuild"]
        self.assertEqual(f["tier"], "attributed cause")
        self.assertIn("prefix changed", f["detail"])
        self.assertEqual(f["evidence"][0]["tokens"], 20_400)
        self.assertEqual(rebuilds, [])

    def test_expected_rebuilds_are_reported_not_waste(self):
        for sid, why in (("compact", "compaction"), ("switch", "model switch")):
            with self.subTest(sid):
                causes, rebuilds = self._run(sid)
                self.assertNotIn("cache_miss_rebuild", causes)
                self.assertEqual(len(rebuilds), 1)
                self.assertIn(why, rebuilds[0]["reason"])
                self.assertEqual(rebuilds[0]["tokens"], 20_400)
                self.assertGreater(rebuilds[0]["usd"], 0)

    def test_gap_past_the_ttl_is_idle_gap_not_a_rebuild(self):
        causes, rebuilds = self._run("ttl")
        self.assertIn("idle_gap", causes)
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual(rebuilds, [])

    def test_idle_gap_owns_long_gaps_one_claim_per_turn(self):
        causes, rebuilds = self._run("idle")
        self.assertIn("idle_gap", causes)
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual(rebuilds, [])
        # a rebuild turn is claimed once: total claimed never exceeds the turn's creation cost
        for sid in FIXTURES:
            found, _ = self._run(sid)
            self.assertLessEqual(len([c for c in found if c != "uncached_input"]), 2, sid)

    def test_expiry_uses_the_cached_prefixs_tier(self):
        turn = {"cache_creation_tokens": 20_000, "cache_read_tokens": 0, "cache_creation_1h_tokens": 0,
                "timestamp": _ts(13), "is_compact_summary": 0, "model": MODEL}
        prev = {"timestamp": _ts(3), "compact": 0, "model": MODEL, "prefix": 20_000}
        # 10 min later: expired for a 5m prefix, still warm for a 1h prefix (even if the rewrite is 5m)
        self.assertEqual(waste._rebuild_reason(turn, {**prev, "ttl": 300})[0], "expected")
        self.assertEqual(waste._rebuild_reason(turn, {**prev, "ttl": 3600})[0], "unexpected")


if __name__ == "__main__":
    unittest.main()
