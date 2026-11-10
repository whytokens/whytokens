"""deferred_tool_load: ToolSearch schemas loaded but unused, reloaded or duplicated.

Fixtures go through the real scanner path. scripts/falsify_deferred_tools.py breaks the
dedupe, the per-lane scoping and the per-lane call check and checks this file goes red.
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import waste
from sources import ClaudeSource, scan_all
from tests.test_waste import _rec, _ts

BIG = 60_000  # tokens the loaded schemas add to the next write


def _result(sid, n, names, total=531, twice=True):
    """The user record carrying a ToolSearch result; written twice, with and without toolUseResult."""
    rec = {"type": "user", "sessionId": sid, "timestamp": _ts(n), "cwd": "/home/dev/acme/app",
           "message": {"role": "user", "content": [{
               "type": "tool_result", "tool_use_id": f"tu-{sid}-{n}-0",
               "content": [{"type": "tool_reference", "tool_name": x} for x in names]}]}}
    out = [json.dumps({**rec, "toolUseResult": {"matches": names, "query": "SECRET QUERY",
                                                 "total_deferred_tools": total}})]
    return out + [json.dumps(rec)] if twice else out


def _lane(sid, names, called, side=False, agent=None, start=0, compact_after=None, extra=()):
    """Opening write, a ToolSearch turn, the schema write, then quiet cached turns."""
    kw = {"side": side, "agent": agent}
    n0 = start
    lines = [_rec(sid, n0 + 1, _ts(n0), read=0, create=20_000, **kw),
             _rec(sid, n0 + 2, _ts(n0 + 1), read=20_000, create=400, tools=[("ToolSearch", {"query": "x"})], **kw)]
    lines += _result(sid, n0 + 2, names)
    lines.append(_rec(sid, n0 + 3, _ts(n0 + 2), read=20_400, create=BIG, tools=[(c, {}) for c in called], **kw))
    ctx = 20_400 + BIG
    for k in range(4, 12):
        lines.append(_rec(sid, n0 + k, _ts(n0 + k - 1), read=ctx, create=400, compact=(k == compact_after), **kw))
        ctx += 400
    return lines + list(extra)


def _reload(sid, compact):
    """One tool loaded and used, then loaded again 3 turns later (a compaction in between if asked)."""
    name = "mcp__a__t"
    lines = [_rec(sid, 1, _ts(0), read=0, create=20_000),
             _rec(sid, 2, _ts(1), read=20_000, create=400, tools=[("ToolSearch", {})])] + _result(sid, 2, [name])
    lines += [_rec(sid, 3, _ts(2), read=20_400, create=BIG, tools=[(name, {})]),
              _rec(sid, 4, _ts(3), read=80_400, create=400, compact=compact),
              _rec(sid, 5, _ts(4), read=80_800, create=400, tools=[("ToolSearch", {})])] + _result(sid, 5, [name])
    lines.append(_rec(sid, 6, _ts(5), read=81_200, create=BIG, tools=[(name, {})]))
    ctx = 141_200
    for k in range(7, 14):
        lines.append(_rec(sid, k, _ts(k), read=ctx, create=400))
        ctx += 400
    return lines


FIVE = [f"mcp__ebay__tool{i}" for i in range(5)]
FIXTURES = {
    "kw": _lane("kw", FIVE, [FIVE[0]]),
    "kwc": _lane("kwc", FIVE, [FIVE[0]], compact_after=6),
    "select": _lane("select", FIVE[:2], FIVE[:2]),
    "dupinstall": _lane("dupinstall", ["mcp__browser__click", "mcp__plugin_x_browser__click"],
                        ["mcp__browser__click", "mcp__plugin_x_browser__click"]),
    "overlap": _lane("overlap", ["mcp__chrome__click", "mcp__playwright__click"],
                     ["mcp__chrome__click", "mcp__playwright__click"]),
    "reload": _reload("reload", False),
    "reloadc": _reload("reloadc", True),
    # two subagent lanes each using a different server's click: no overlap across lanes
    "subsx": (_lane("subsx", ["mcp__a__click"], ["mcp__a__click"], side=True, agent="ag1")
              + _lane("subsx", ["mcp__b__click"], ["mcp__b__click"], side=True, agent="ag2", start=40)),
    # two subagent lanes each load and use the same tool: an observation, never waste
    "subs": (_lane("subs", ["mcp__a__t"], ["mcp__a__t"], side=True, agent="ag1")
             + _lane("subs", ["mcp__a__t"], ["mcp__a__t"], side=True, agent="ag2", start=40)),
}


def _patched(sid, patch):
    """A "kw"-shaped lane with some records rewritten: patch(index, record) edits in place."""
    lines = _lane(sid, FIVE, [FIVE[0]])
    for i, line in enumerate(lines):
        rec = json.loads(line)
        patch(i, rec)
        lines[i] = json.dumps(rec)
    return lines


def _compact_next(i, rec):  # lines[4] is the schema-write turn right after the ToolSearch result
    if i == 4:
        rec["isCompactSummary"] = True


def _two_rewrites(i, rec):  # two later quiet turns become full rewrites (no cache read, claimed elsewhere)
    if i in (6, 7):
        rec["message"]["usage"].update(cache_read_input_tokens=0, cache_creation_input_tokens=90_000)


FIXTURES["kwc0"] = _patched("kwc0", _compact_next)
FIXTURES["kwrw"] = _patched("kwrw", _two_rewrites)


class TestDeferredToolLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        proj = Path(cls.tmp) / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        for sid, lines in FIXTURES.items():
            (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cls.db = str(Path(cls.tmp) / "usage.db")
        scan_all(db_path=cls.db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(proj.parent)])])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _find(self, sid):
        return {f["cause"]: f for f in waste.report(self.db, sid, min_share=0)}.get("deferred_tool_load")

    def test_keyword_search_unused_schemas_priced(self):
        f = self._find("kw")
        self.assertIn("4 of 5", f["detail"])
        self.assertEqual(f["tier"], "inferred")
        self.assertEqual(f["fix"], "Use select:<name> instead of keyword search.")
        self.assertGreater(f["usd"], 0)
        self.assertEqual(len(f["evidence"]), 1)

    def test_select_only_session_is_silent(self):
        self.assertIsNone(self._find("select"))

    def test_duplicate_install_flagged(self):
        f = self._find("dupinstall")
        self.assertIn("duplicate another MCP server", f["detail"])
        self.assertIn("Deduplicate the MCP install", f["fix"])

    def test_overlapping_servers_flagged(self):
        self.assertIn("Remove one of the overlapping servers chrome / playwright", self._find("overlap")["fix"])

    def test_reload_while_in_context_flagged_but_not_after_compaction(self):
        self.assertIn("1 loaded again while still in context", self._find("reload")["detail"])
        self.assertIsNone(self._find("reloadc"))

    def test_charge_stops_at_compaction(self):
        self.assertEqual(self._find("kw")["evidence"][0]["tokens"], 4 * self._find("kwc")["evidence"][0]["tokens"])

    def test_compaction_on_the_next_turn_charges_nothing(self):
        # combined review: `k > i + 1` skipped a compaction on the very next turn
        self.assertIsNone(self._find("kwc0"))

    def test_later_turns_without_a_cache_read_are_not_charged(self):
        # combined review: 8 quiet turns after the schema write in "kw"; two become rewrites in "kwrw"
        kw, rw = self._find("kw")["evidence"][0]["tokens"], self._find("kwrw")["evidence"][0]["tokens"]
        self.assertEqual(rw * 8, kw * 6)

    def test_overlap_is_judged_per_lane(self):
        self.assertIsNone(self._find("subsx"))

    def test_subagent_lanes_are_not_loaded_twice(self):
        self.assertIsNone(self._find("subs"))

    def test_stored_names_deduped_by_tool_use_id_and_no_query_text(self):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT loaded_tools, total_deferred_tools FROM tool_results "
                                "WHERE loaded_tools IS NOT NULL AND session_id = 'kw'").fetchall()
            dump = "".join(str(r) for r in conn.execute("SELECT * FROM tool_results"))
        finally:
            conn.close()
        self.assertEqual([(json.loads(a), b) for a, b in rows], [(FIVE, 531)])
        self.assertNotIn("SECRET QUERY", dump)


if __name__ == "__main__":
    unittest.main()


class TestDeferredBackfillRetry(unittest.TestCase):
    """Combined review: an unreadable transcript must leave the backfill marker unset, so it retries."""
    def test_unreadable_transcript_leaves_the_marker_unset(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        proj = tmp / "projects" / "home-dev-acme-app"
        proj.mkdir(parents=True)
        (proj / "b.jsonl").write_text("\n".join(_lane("b", FIVE, [FIVE[0]])) + "\n", encoding="utf-8")
        src = ClaudeSource(projects_dirs=[str(proj.parent)])
        db = str(tmp / "usage.db")
        scan_all(db_path=db, verbose=False, sources=[src])
        c = sqlite3.connect(db)
        self.addCleanup(c.close)
        c.execute("DELETE FROM schema_meta WHERE key = 'deferred_loads_backfill_done_claude'")
        c.commit()
        src.discover_files = lambda: [str(proj / "missing.jsonl")]
        src.backfill_deferred_loads(c)
        self.assertIsNone(c.execute(
            "SELECT 1 FROM schema_meta WHERE key = 'deferred_loads_backfill_done_claude'").fetchone())
        del src.discover_files
        src.backfill_deferred_loads(c)
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM schema_meta WHERE key = 'deferred_loads_backfill_done_claude'").fetchone())
