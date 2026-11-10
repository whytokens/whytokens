"""compaction marker on user records, partial 5m/1h expiry, TTL boundary, idle_gap keyed on the TTL."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sources
import waste
from sources.claude import ClaudeSource
from tests.test_waste import MODEL, _ts

ROOT = Path(__file__).resolve().parent.parent
PRICING_JS = (ROOT / "static" / "modules" / "pricing.js").as_uri()


def _asst(sid, n, minute, read, create, c1h=0):
    """Assistant record with an explicit 5m/1h split of the cache write."""
    return json.dumps({
        "type": "assistant", "sessionId": sid, "timestamp": _ts(minute), "cwd": "/home/dev/acme/app",
        "message": {"id": f"msg-{sid}-{n}", "model": MODEL, "content": [{"type": "text", "text": "."}],
                    "usage": {"input_tokens": 100, "output_tokens": 100, "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": create,
                              "cache_creation": {"ephemeral_5m_input_tokens": create - c1h,
                                                 "ephemeral_1h_input_tokens": c1h}}}})


def _compact_user(sid, minute):
    return json.dumps({"type": "user", "sessionId": sid, "timestamp": _ts(minute), "cwd": "/home/dev/acme/app",
                       "isCompactSummary": True, "message": {"role": "user", "content": "summary of the session"}})


FIXTURES = {
    # compaction flagged on the USER record; the next assistant turn rebuilds the prefix
    "usercompact": [_asst("usercompact", 1, 0, 0, 20_000), _asst("usercompact", 2, 3, 20_000, 400),
                    _compact_user("usercompact", 5), _asst("usercompact", 3, 6, 0, 20_400),
                    _asst("usercompact", 4, 7, 20_400, 200)],
    # 1h-tier prefix (4k) + 5m-tier growth (16k); 10 min later the 5m part is gone, the 1h part still read
    "partial": [_asst("partial", 1, 0, 0, 4_000, c1h=4_000), _asst("partial", 2, 1, 4_000, 16_000),
                _asst("partial", 3, 11, 4_000, 16_000)],
    # same shape but nothing read back: a changed prefix, not tier expiry
    "fullmiss": [_asst("fullmiss", 1, 0, 0, 20_000, c1h=20_000), _asst("fullmiss", 2, 11, 0, 20_000)],
    # 5m prefix, 6 min idle: past the 5m TTL, so idle_gap (no longer a hardcoded 30 min)
    "idle6": [_asst("idle6", 1, 0, 0, 20_000), _asst("idle6", 2, 6, 0, 20_000)],
    # 1h prefix, 6 min idle: still warm, so no idle claim
    "warm1h": [_asst("warm1h", 1, 0, 0, 20_000, c1h=20_000), _asst("warm1h", 2, 6, 20_000, 400)],
}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        cls.proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (cls.proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        cls.src = ClaudeSource(projects_dirs=[cls.proj.parent])
        sources.scan_all(db_path=cls.db, verbose=False, sources=[cls.src])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_waste(self, sid):
        rebuilds = []
        found = waste.report(self.db, sid, min_share=0, rebuilds=rebuilds)
        return {f["cause"]: f for f in found}, rebuilds

    def flags(self, sid, db=None):
        with closing(sqlite3.connect(db or self.db)) as c:
            return [r[0] for r in c.execute(
                "SELECT is_compact_summary FROM turns WHERE session_id=? ORDER BY timestamp", (sid,))]


class TestUserCompactionMarker(Base):
    def test_flag_lands_on_the_turn_after_the_summary_only(self):
        self.assertEqual(self.flags("usercompact"), [0, 0, 1, 0])

    def test_rebuild_after_compaction_is_expected_not_waste(self):
        causes, rebuilds = self.run_waste("usercompact")
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual([r["reason"] for r in rebuilds], ["context compaction"])

    def test_backfill_fixes_existing_rows_idempotently(self):
        db = str(Path(self.tmp) / "backfill.db")  # a copy: the shared scan must stay as ingested
        shutil.copy(self.db, db)
        with closing(sqlite3.connect(db)) as c:
            c.row_factory = sqlite3.Row
            c.execute("UPDATE turns SET is_compact_summary = 0")
            c.execute("DELETE FROM schema_meta WHERE key = 'compact_backfill_done_claude'")
            c.commit()
            self.assertEqual(self.src.backfill_compact(c), 1)
            self.assertEqual(self.src.backfill_compact(c), 0)  # marker set: no second pass
        self.assertEqual(self.flags("usercompact", db), [0, 0, 1, 0])


class TestIncrementalScan(unittest.TestCase):
    def test_flag_survives_a_scan_boundary_between_summary_and_answer(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proj = tmp / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        f = proj / "inc.jsonl"
        f.write_text("\n".join([_asst("inc", 1, 0, 0, 20_000), _compact_user("inc", 5)]) + "\n", encoding="utf-8")
        db = str(tmp / "usage.db")
        src = ClaudeSource(projects_dirs=[proj.parent])
        sources.scan_all(db_path=db, verbose=False, sources=[src])
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(_asst("inc", 2, 6, 0, 20_000) + "\n")
        sources.scan_all(db_path=db, verbose=False, sources=[src])
        with closing(sqlite3.connect(db)) as c:
            got = [r[0] for r in c.execute("SELECT is_compact_summary FROM turns ORDER BY timestamp")]
        self.assertEqual(got, [0, 1])


class TestPartialExpiry(Base):
    def test_1h_part_cached_5m_part_expired_is_expected(self):
        causes, rebuilds = self.run_waste("partial")
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual(len(rebuilds), 1)
        self.assertIn("5m-tier", rebuilds[0]["reason"])

    def test_full_miss_on_a_1h_prefix_is_still_unexpected(self):
        causes, _ = self.run_waste("fullmiss")
        self.assertIn("cache_miss_rebuild", causes)


class TestBackfillEdges(unittest.TestCase):
    def _run(self, lines, unreadable=False):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proj = tmp / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        (proj / "e.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        src = ClaudeSource(projects_dirs=[proj.parent])
        db = str(tmp / "usage.db")
        sources.scan_all(db_path=db, verbose=False, sources=[src])
        c = sqlite3.connect(db)
        self.addCleanup(c.close)
        c.row_factory = sqlite3.Row
        c.execute("UPDATE turns SET is_compact_summary = 0")
        c.execute("DELETE FROM schema_meta WHERE key = 'compact_backfill_done_claude'")
        c.commit()
        if unreadable:
            (proj / "e.jsonl").write_bytes(b"")  # keep the file but make open() fail below
            src.discover_files = lambda: [str(proj / "missing.jsonl")]
        src.backfill_compact(c)
        flags = [r[0] for r in c.execute("SELECT is_compact_summary FROM turns ORDER BY timestamp")]
        marker = c.execute("SELECT 1 FROM schema_meta WHERE key = 'compact_backfill_done_claude'").fetchone()
        return flags, marker

    def test_zero_usage_assistant_does_not_consume_the_marker(self):
        zero = json.loads(_asst("e", 9, 6, 0, 0))
        zero["message"]["usage"].update(input_tokens=0, output_tokens=0)
        flags, _ = self._run([_asst("e", 1, 0, 0, 20_000), _compact_user("e", 5), json.dumps(zero),
                              _asst("e", 2, 7, 0, 20_000)])
        self.assertEqual(flags, [0, 1])

    def test_unreadable_transcript_leaves_the_marker_unset(self):
        _, marker = self._run([_asst("e", 1, 0, 0, 20_000)], unreadable=True)
        self.assertIsNone(marker)


class TestTtlBoundary(unittest.TestCase):
    def _reason(self, gap_s, ttl):
        turn = {"cache_creation_tokens": 20_000, "cache_read_tokens": 0, "timestamp": _ts(0, gap_s),
                "is_compact_summary": 0, "model": MODEL}
        prev = {"timestamp": _ts(0), "compact": 0, "model": MODEL, "prefix": 20_000, "ttl": ttl}
        return waste._rebuild_reason(turn, prev)[0]

    def test_python_gap_equal_to_ttl_is_expired(self):
        self.assertEqual(self._reason(300, 300), "expected")
        self.assertEqual(self._reason(299, 300), "unexpected")
        self.assertEqual(self._reason(3600, 3600), "expected")
        self.assertEqual(self._reason(3599, 3600), "unexpected")

    def test_js_rewrite_kinds_agrees(self):
        turns = [{"actor": "main", "cache_creation": 1, "gap_s": g, "cache_creation_1h": h}
                 for g, h in ((None, 0), (300, 0), (299, 0), (3600, 5), (3599, 5))]
        code = (f"import {{ rewriteKinds }} from {json.dumps(PRICING_JS)};"
                f"console.log(JSON.stringify(rewriteKinds({json.dumps(turns)})));")
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["opening", "cold", None, "cold", None])


class TestIdleGapKeysOnTtl(Base):
    def test_6_min_idle_on_a_5m_prefix_is_idle_gap_with_one_claim(self):
        causes, rebuilds = self.run_waste("idle6")
        self.assertIn("idle_gap", causes)
        self.assertNotIn("cache_miss_rebuild", causes)
        self.assertEqual(rebuilds, [])

    def test_6_min_on_a_1h_prefix_is_not_idle(self):
        causes, _ = self.run_waste("warm1h")
        self.assertNotIn("idle_gap", causes)


if __name__ == "__main__":
    unittest.main()
