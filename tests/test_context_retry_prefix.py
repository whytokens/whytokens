"""large_context, retry_loop and fixed_prefix: fixtures through the real scanner path.

Each detector: a known-clean session scores zero and a constructed positive scores the exact
expected dollars. scripts/falsify_context_retry_prefix.py breaks one rule at a time and checks
this file goes red.
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import api
import policy
import pricing
import waste
from sources import ClaudeSource, scan_all
from tests.test_drilldown import DRILL_URL, NODE, _node
from tests.test_waste import MODEL, _rec, _ts

CWD = "/home/dev/acme/app"


def _prompt(sid, n, text="go"):
    return json.dumps({"type": "user", "sessionId": sid, "timestamp": _ts(n), "cwd": CWD,
                       "message": {"role": "user", "content": text}})


def _result(sid, n, err, i=0, side=False, agent=None):
    """An empty tool_result (0 bytes: resident_output claims nothing) with its is_error flag."""
    return json.dumps({"type": "user", "sessionId": sid, "timestamp": _ts(n), "cwd": CWD, "isSidechain": side,
                       **({"agentId": agent} if agent else {}),
                       "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu-{sid}-{n}-{i}",
                                                                "content": "", "is_error": err}]}})


def _call(sid, n, tool, arg, err, read, **kw):
    return [_rec(sid, n, _ts(n), read=read, create=400, tools=[(tool, arg)], **kw), _result(sid, n, err, **kw)]


def _bash(c):
    return ("Bash", {"command": c})


def _retry(sid, steps):
    """One prompt, then one call per step: (tool, input, failed)."""
    lines, ctx = [_prompt(sid, 0), _rec(sid, 1, _ts(1), read=0, create=20_000)], 20_000
    for k, (tool, arg, err) in enumerate(steps, start=2):
        lines += _call(sid, k, tool, arg, err, ctx)
        ctx += 400
    return lines + [_rec(sid, 99, _ts(99), read=ctx, create=400)]


def _streamed_edit(sid):
    """Fail, then one message streamed as two records (an Edit, then a Read: the stored turn keeps only
    the Read), then fail twice more. The Edit sits between the attempts, so no retry loop."""
    lines = _retry(sid, [(*_bash("pytest -q"), True)])[:-1]
    first = _rec(sid, 3, _ts(3), read=20_400, create=400, tools=[("Edit", {"file_path": "/src/a.py"})])
    second = _rec(sid, 3, _ts(3), read=20_400, create=400, tools=[("Read", {"file_path": "/src/b.py"})]).replace(
        f"tu-{sid}-3-0", f"tu-{sid}-3-1")
    lines += [first, _result(sid, 3, False), second, _result(sid, 3, False, i=1)]
    for k in (4, 5):
        lines += _call(sid, k, *_bash("pytest -q"), True, 20_400 + 400 * k)
    return lines


def _streamed_failure(sid):
    """Fail, then one message streamed as two records (another command failing, then a Read: the stored
    turn keeps only the Read), then fail twice more. Another failure sits between the attempts."""
    lines = _retry(sid, [(*_bash("pytest -q"), True)])[:-1]
    first = _rec(sid, 3, _ts(3), read=20_400, create=400, tools=[_bash("ls /nope")])
    second = _rec(sid, 3, _ts(3), read=20_400, create=400, tools=[("Read", {"file_path": "/src/b.py"})]).replace(
        f"tu-{sid}-3-0", f"tu-{sid}-3-1")
    lines += [first, _result(sid, 3, True), second, _result(sid, 3, False, i=1)]
    for k in (4, 5):
        lines += _call(sid, k, *_bash("pytest -q"), True, 20_400 + 400 * k)
    return lines


def _big_results(lines, size=60_000):
    """The same session with every tool result ``size`` bytes long, so resident_output claims cache writes too."""
    return [x.replace('"content": ""', '"content": "%s"' % ("x" * size)) for x in lines]


def _chain(sid, contexts, compact_at=(), side=None):
    """Main-lane turns whose context (input 200 + read + write) follows ``contexts``."""
    lines, prev = [], 0
    for k, ctx in enumerate(contexts, start=1):
        body = ctx - 200
        read = min(prev, body) if k not in compact_at else 0
        lines.append(_rec(sid, k, _ts(k), read=read, create=body - read, compact=k in compact_at))
        prev = body
    return lines


FIXTURES = {
    # large_context
    "lc_clean": _chain("lc_clean", [100_000, 150_000, 200_000, 240_000, 249_000]),
    "lc": _chain("lc", [200_200, 260_400, 260_800]),
    "lc_compact": _chain("lc_compact", [200_200, 260_000, 30_000, 255_000], compact_at=(3,)),
    # retry_loop
    "rt_clean": _retry("rt_clean", [(*_bash("pytest -q"), False)] * 4),
    "rt_two": _retry("rt_two", [(*_bash("pytest -q"), True)] * 2),
    "rt": _retry("rt", [(*_bash("pytest  -q"), True), (*_bash("pytest -q"), True), (*_bash(" pytest -q\n"), True)]),
    "rt_path": _retry("rt_path", [("Read", {"file_path": "/src/missing.py"}, True)] * 3),
    "rt_edit": _retry("rt_edit", [(*_bash("pytest -q"), True), ("Edit", {"file_path": "/src/a.py"}, False),
                                  (*_bash("pytest -q"), True), ("Edit", {"file_path": "/src/a.py"}, False),
                                  (*_bash("pytest -q"), True)]),
    "rt_read_between": _retry("rt_read_between", [(*_bash("make"), True), ("Read", {"file_path": "/src/log"}, False),
                                                  (*_bash("make"), True), (*_bash("make"), True)]),
    "rt_streamed": _streamed_edit("rt_streamed"),
    "rt_streamed_failure": _streamed_failure("rt_streamed_failure"),
    "rt_unpriced": [x.replace(MODEL, "local-model-without-a-price") for x in _retry("rt_unpriced", [(*_bash("make"), True)] * 4)],
    "rt_big": _big_results(_retry("rt_big", [(*_bash("make"), True)] * 4)),
    # a subagent lane has no prompt boundaries (prompt_seq is NULL): not measured, never one big run
    "rt_side": [_prompt("rt_side", 0), _rec("rt_side", 1, _ts(1), read=0, create=20_000)] + [
        x for k in (2, 3, 4) for x in _call("rt_side", k, *_bash("pytest -q"), True, 20_000, side=True, agent="ag1")],
    # fixed_prefix
    "fp_clean": _chain("fp_clean", [20_200, 20_600, 21_000, 21_400]),
    "fp": _chain("fp", [60_200, 60_600, 61_000, 61_400, 61_800]),
    # a subagent lane with its own big prefix: subagent_preamble's, never part of the main lane's fixed_prefix
    "fp_side": _chain("fp_side", [60_200, 60_600, 61_000]) + [
        _rec("fp_side", 10 + k, _ts(10 + k), read=0 if k == 0 else 80_000, create=80_000 if k == 0 else 400,
             side=True, agent="ag1") for k in range(4)],
}


def _read(tokens, n):
    return pricing.calc_cost(MODEL, 0, 0, tokens, 0, _ts(n))


class _Scan(unittest.TestCase):
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

    def find(self, sid, cause, **kw):
        kw.setdefault("min_share", 0)
        kw.setdefault("min_usd", 0)
        return [f for f in waste.report(self.db, session_id=sid, **kw) if f["cause"] == cause]



def _legacy_trim(db, sids):
    """Rows ingested before a streamed message's calls were merged keep only the last record's call.
    Their transcripts can be gone, so a re-parse never fixes them: the tool_results fallback must."""
    conn = sqlite3.connect(db)
    for sid in sids:
        conn.execute("UPDATE turns SET tool_calls = json_array(json_extract(tool_calls, '$[#-1]'))"
                     " WHERE session_id = ? AND json_array_length(tool_calls) > 1", (sid,))
    conn.commit()
    conn.close()

class TestLargeContext(_Scan):
    def test_clean_session_scores_zero(self):
        self.assertEqual(self.find("lc_clean", "large_context"), [])

    def test_tokens_above_threshold_priced_at_the_cache_read_rate(self):
        [f] = self.find("lc", "large_context")
        self.assertAlmostEqual(f["usd"], _read(10_400, 2) + _read(10_800, 3), places=9)
        self.assertEqual(f["tier"], "observation")
        self.assertFalse(f["in_total"])
        self.assertEqual(f["evidence"][0]["message_id"], "msg-lc-2")  # the crossing turn leads
        a = f["action"]
        self.assertEqual(a["id"], "split_or_compact")
        self.assertEqual([c["message_id"] for c in a["calls"]], ["msg-lc-2"])
        self.assertIn("250K", a["rule"])

    def test_compaction_resets_the_crossing(self):
        [f] = self.find("lc_compact", "large_context")
        self.assertEqual([c["message_id"] for c in f["action"]["calls"]], ["msg-lc_compact-2", "msg-lc_compact-4"])
        self.assertAlmostEqual(f["usd"], _read(10_000, 2) + _read(5_000, 4), places=9)


class TestRetryLoop(_Scan):
    def test_clean_and_short_runs_score_zero(self):
        for sid in ("rt_clean", "rt_two"):
            self.assertEqual(self.find(sid, "retry_loop"), [], sid)

    def test_second_and_later_failures_are_priced_never_the_first(self):
        [f] = self.find("rt", "retry_loop")
        # turns 3 and 4 issued the 2nd and 3rd failures: their output (300) plus their write (400)
        self.assertAlmostEqual(f["usd"], sum(pricing.calc_cost(MODEL, 0, 300, 0, 400, _ts(n)) for n in (3, 4)), places=9)
        self.assertEqual(f["tier"], "inferred")
        self.assertTrue(f["in_total"])
        self.assertEqual({e["message_id"] for e in f["evidence"]}, {"msg-rt-3", "msg-rt-4"})
        self.assertIn("failed 3 times", f["detail"])
        self.assertIn("Bash", f["fix"])

    def test_same_path_run_names_the_file(self):
        [f] = self.find("rt_path", "retry_loop")
        self.assertIn("missing.py", f["fix"])
        self.assertNotIn("/src/", f["fix"])  # basenames only: full paths carry usernames

    def test_edit_between_attempts_is_an_edit_test_loop_not_a_retry(self):
        self.assertEqual(self.find("rt_edit", "retry_loop"), [])

    def test_an_edit_lost_from_a_streamed_message_still_breaks_the_run(self):
        self.assertEqual(self.find("rt_streamed", "retry_loop"), [])

    def test_a_failure_lost_from_a_streamed_message_still_breaks_the_run(self):
        self.assertEqual(self.find("rt_streamed_failure", "retry_loop"), [])


class TestLegacyStreamedRows(_Scan):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _legacy_trim(cls.db, ("rt_streamed", "rt_streamed_failure"))

    def test_the_trim_really_lost_calls(self):  # guards the fixture: otherwise the tests below prove nothing
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM tool_results r WHERE r.session_id IN ('rt_streamed','rt_streamed_failure')"
                         " AND NOT EXISTS (SELECT 1 FROM turns t, json_each(t.tool_calls) j WHERE t.message_id = r.message_id"
                         " AND json_extract(j.value, '$.id') = r.call_id)").fetchone()[0]
        conn.close()
        self.assertGreater(n, 0)

    def test_an_edit_lost_from_an_old_row_still_breaks_the_run(self):
        self.assertEqual(self.find("rt_streamed", "retry_loop"), [])

    def test_a_failure_lost_from_an_old_row_still_breaks_the_run(self):
        self.assertEqual(self.find("rt_streamed_failure", "retry_loop"), [])

    def test_a_run_on_a_model_without_a_price_is_no_finding_and_no_crash(self):
        self.assertEqual(self.find("rt_unpriced", "retry_loop"), [])
        self.assertIsInstance(waste.report(self.db, min_share=0, min_usd=0), list)  # the whole-DB report verify uses

    def test_cache_write_another_cause_claimed_is_not_priced_again(self):
        found = waste.report(self.db, session_id="rt_big", min_share=0, min_usd=0)
        [retry] = [f for f in found if f["cause"] == "retry_loop"]
        claimed = {}
        for f in found:
            if f["cause"] in ("resident_output", "retry_loop"):
                for e in f["evidence"]:
                    claimed.setdefault(e["message_id"], {})[f["cause"]] = e["tokens"]
        shared = [m for m, c in claimed.items() if len(c) == 2]
        self.assertTrue(shared)  # the fixture really has both causes on one turn
        for m in shared:  # output 300 + write 400 is all the turn has to attribute
            self.assertLessEqual(sum(claimed[m].values()), 700, m)
        self.assertLess(retry["usd"], sum(pricing.calc_cost(MODEL, 0, 300, 0, 400, _ts(n)) for n in (3, 4, 5)))

    def test_subagent_turns_without_prompt_boundaries_are_not_a_run(self):
        self.assertEqual(self.find("rt_side", "retry_loop"), [])

    def test_a_successful_read_between_attempts_keeps_the_run(self):
        self.assertEqual(len(self.find("rt_read_between", "retry_loop")), 1)

    def test_error_flag_and_command_hash_are_stored(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        try:
            flags = dict(c.execute("SELECT call_id, is_error FROM tool_results WHERE session_id='rt_clean'"))
            calls = [json.loads(r[0]) for r in c.execute(
                "SELECT tool_calls FROM turns WHERE session_id='rt' AND tool_calls LIKE '%Bash%' ORDER BY timestamp")]
        finally:
            c.close()
        self.assertEqual(set(flags.values()), {0})
        hashes = {x[0]["input"]["command_hash"] for x in calls}
        self.assertEqual(hashes, {policy.command_hash("pytest -q")})  # whitespace-normalised
        self.assertNotIn("pytest", json.dumps(calls))  # the command itself is never stored

    def test_freshly_scanned_sessions_are_all_measured(self):
        cell = waste.measurement(self.db)["causes"]["retry_loop"]["claude"]
        self.assertEqual(cell["status"], "measured")


class TestFixedPrefix(_Scan):
    def test_lean_prefix_scores_zero(self):
        self.assertEqual(self.find("fp_clean", "fixed_prefix"), [])

    def test_opening_write_plus_prefix_reread_on_every_later_turn(self):
        [f] = self.find("fp", "fixed_prefix")
        want = pricing.calc_cost(MODEL, 0, 0, 0, 60_000, _ts(1)) + sum(_read(60_000, n) for n in (2, 3, 4, 5))
        self.assertAlmostEqual(f["usd"], want, places=9)
        self.assertIn("over 4 turns", f["detail"])
        self.assertFalse(f["in_total"])

    def test_a_subagent_lane_never_produces_fixed_prefix(self):
        [f] = self.find("fp_side", "fixed_prefix")  # the main lane's only: 3 main turns, the 4 subagent turns ignored
        want = pricing.calc_cost(MODEL, 0, 0, 0, 60_000, _ts(1)) + sum(_read(60_000, n) for n in (2, 3))
        self.assertAlmostEqual(f["usd"], want, places=9)
        self.assertTrue(all(e["agent_id"] is None for e in f["evidence"]))
        self.assertTrue(self.find("fp_side", "subagent_preamble"))


class TestObservationsStayOutOfTotals(_Scan):
    def test_api_totals_exclude_observations(self):
        for sid in ("fp", "lc"):
            p = api.get_waste(db_path=self.db, session_id=sid)
            obs = [f for f in p["findings"] if f["cause"] in waste.UNCOUNTED]
            self.assertTrue(obs, sid)
            self.assertAlmostEqual(p["total_usd"], sum(f["usd"] for f in p["findings"] if f["in_total"]))
        whole = api.get_waste(db_path=self.db)
        self.assertAlmostEqual(whole["total_usd"], sum(f["usd"] for f in whole["findings"] if f["cause"] not in waste.UNCOUNTED))

    @unittest.skipUnless(NODE, "node not installed")
    def test_overhead_index_total_skips_observations(self):
        out = _node(f"""
import {{ overheadIndex, overheadOf }} from '{DRILL_URL}';
const idx = overheadIndex([{{ session_id: 'a', cause: 'idle_gap', usd: 1 }},
                           {{ session_id: 'a', cause: 'fixed_prefix', usd: 50, in_total: false }}]);
console.log(overheadOf(idx, 'a'), overheadOf(idx, 'a', 'fixed_prefix'));
""")
        self.assertEqual(out, "1 50")


@unittest.skipUnless(NODE, "node not installed")
class TestWhereToActSkipsObservations(unittest.TestCase):
    def test_observation_is_neither_in_the_strip_nor_the_overhead_card(self):
        from tests.test_drilldown import TestWhereToAct
        f = lambda cause, usd, counted=True: {"session_id": "s1", "cause": cause, "usd": usd, "tier": "observed",
                                               "attribution": "observed", "session_usd": 50, "fix": "", "evidence": [],
                                               "action": None, "in_total": counted}
        got = json.loads(TestWhereToAct._run(self, [f("a", 1), f("b", 2), f("c", 90, False)], ["s1"]))
        self.assertNotIn("Cause C", got["slot"])
        self.assertEqual(got["value"], "$3.00")


class TestWhatifAndObservationsStayOutOfTotals(_Scan):
    """Two lanes, one invariant: a model what-if and the observation causes are both absent from
    the attributed overhead total (and so the headline and where-to-act, which read only counted findings)."""

    def test_reprice_leaves_the_waste_payload_and_its_counted_total_alone(self):
        import whatif
        before = json.dumps(api.get_waste(self.db), sort_keys=True, default=str)
        whatif.reprice(whatif.NEXT_TIER, db_path=Path(self.db))
        payload = api.get_waste(self.db)
        self.assertEqual(json.dumps(payload, sort_keys=True, default=str), before)
        self.assertNotIn("repriced", before)
        obs = [f for f in payload["findings"] if f["cause"] in waste.UNCOUNTED]
        self.assertTrue(obs)
        self.assertTrue(all(not f["in_total"] for f in obs))
        self.assertAlmostEqual(payload["total_usd"], sum(f["usd"] for f in payload["findings"] if f["in_total"]))
        self.assertAlmostEqual(sum(r["usd"] for r in payload["rollup"] if r["in_total"]), payload["total_usd"])
        self.assertLess(payload["total_usd"], sum(f["usd"] for f in payload["findings"]))  # observations really excluded


class TestRetryInputsBackfill(unittest.TestCase):
    """Rows scanned before is_error / command_hash existed: the scan backfills them from transcripts
    still on disk, in place, and leaves sessions whose transcript is gone unmeasured."""

    TOKENS = ("SELECT message_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
              "cache_creation_1h_tokens FROM turns ORDER BY message_id")

    def setUp(self):
        import sqlite3
        from sources.claude import RETRY_INPUTS_MARKER
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "projects" / "home-dev-acme-app"
        self.proj.mkdir(parents=True)
        for sid in ("bf_keep", "bf_gone"):
            (self.proj / f"{sid}.jsonl").write_text("\n".join(_retry(sid, [(*_bash("pytest -q"), True)] * 4)) + "\n", encoding="utf-8")
        (self.proj / "bf_path.jsonl").write_text(  # a path-keyed loop: its calls need only their ids back
            "\n".join(_retry("bf_path", [("Read", {"file_path": "/src/missing.py"}, True)] * 3)) + "\n", encoding="utf-8")
        self.db = str(Path(self.tmp) / "usage.db")
        self.scan()
        c = sqlite3.connect(self.db)
        self.flags = dict(c.execute("SELECT call_id, is_error FROM tool_results"))
        # turn the DB into one scanned before the two fields existed
        c.execute("UPDATE tool_results SET is_error = NULL")
        for mid, calls in c.execute("SELECT message_id, tool_calls FROM turns WHERE tool_calls IS NOT NULL").fetchall():
            stripped = [{**x, "input": {k: v for k, v in x["input"].items() if k != "command_hash"}} for x in json.loads(calls)]
            if mid.endswith("-3") or "bf_path" in mid:  # rows from before call ids were stored have no id to match on
                stripped = [{k: v for k, v in x.items() if k != "id"} for x in stripped]
            c.execute("UPDATE turns SET tool_calls = ? WHERE message_id = ?", (json.dumps(stripped), mid))
        # values that are already there must survive the backfill untouched
        c.execute("UPDATE tool_results SET is_error = 7 WHERE call_id = 'tu-bf_keep-2-0'")
        c.execute("UPDATE turns SET tool_calls = REPLACE(tool_calls, '\"command_len\"', '\"command_hash\": \"keepme\", \"command_len\"') "
                  "WHERE message_id = 'msg-bf_keep-2'")
        c.execute("DELETE FROM schema_meta WHERE key = ?", (RETRY_INPUTS_MARKER,))
        c.commit()
        self.before = self.dump(c)
        c.close()
        (self.proj / "bf_gone.jsonl").unlink()  # pruned: only the DB remembers it

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        scan_all(db_path=self.db, verbose=False, sources=[ClaudeSource(projects_dirs=[str(self.proj.parent)])])

    def dump(self, c):
        return {"tokens": c.execute(self.TOKENS).fetchall(),
                "counts": [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("turns", "tool_results", "sessions")],
                "sessions": c.execute("SELECT %s FROM sessions ORDER BY session_id" % ", ".join(  # shape_* re-derive when tool_calls are backfilled
                    r[1] for r in c.execute("PRAGMA table_info(sessions)") if not r[1].startswith("shape"))).fetchall(),
                "flags": dict(c.execute("SELECT call_id, is_error FROM tool_results")),
                "calls": dict(c.execute("SELECT message_id, tool_calls FROM turns"))}

    def after(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        try:
            return self.dump(c)
        finally:
            c.close()

    def test_fills_nulls_from_transcripts_still_on_disk_and_nothing_else(self):
        self.scan()
        got = self.after()
        for cid, flag in got["flags"].items():
            if cid == "tu-bf_keep-2-0":
                self.assertEqual(flag, 7)  # already set: never overwritten
            elif "bf_keep" in cid or "bf_path" in cid:
                self.assertEqual(flag, self.flags[cid], cid)
            else:
                self.assertIsNone(flag, cid)  # transcript gone: unmeasured, never read as success
        self.assertIn('"keepme"', got["calls"]["msg-bf_keep-2"])
        self.assertIn(policy.command_hash("pytest -q"), got["calls"]["msg-bf_keep-4"])
        self.assertIn(policy.command_hash("pytest -q"), got["calls"]["msg-bf_keep-3"])  # no stored id: matched by position
        self.assertIn('"id"', got["calls"]["msg-bf_keep-3"])  # restored too: the detector joins on it
        self.assertNotIn("command_hash", got["calls"]["msg-bf_gone-3"])
        for key in ("tokens", "counts", "sessions"):
            self.assertEqual(got[key], self.before[key], key)  # no row added, removed or re-priced

    def rewrite(self, mid, fn):
        import sqlite3
        c = sqlite3.connect(self.db)
        calls = json.loads(c.execute("SELECT tool_calls FROM turns WHERE message_id = ?", (mid,)).fetchone()[0])
        c.execute("UPDATE turns SET tool_calls = ? WHERE message_id = ?", (json.dumps([fn(x) for x in calls]), mid))
        c.commit()
        c.close()

    def test_row_without_id_is_not_paired_with_a_call_of_another_length(self):
        # same tool at the same position, but the stored metadata describes a different command
        self.rewrite("msg-bf_keep-3", lambda x: {**x, "input": {**x["input"], "command_len": x["input"]["command_len"] + 14}})
        self.scan()
        got = self.after()["calls"]["msg-bf_keep-3"]
        self.assertNotIn("command_hash", got)
        self.assertNotIn('"id"', got)

    def test_row_without_id_that_already_has_its_hash_gets_the_id_back(self):
        h = policy.command_hash("pytest -q")
        self.rewrite("msg-bf_keep-3", lambda x: {**x, "input": {**x["input"], "command_hash": h}})
        self.scan()
        self.assertIn('"id"', self.after()["calls"]["msg-bf_keep-3"])
        self.rewrite("msg-bf_keep-3", lambda x: {**{k: v for k, v in x.items() if k != "id"},
                                               "input": {**x["input"], "command_hash": "0" * 16}})
        import sqlite3
        from sources.claude import RETRY_INPUTS_MARKER
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM schema_meta WHERE key = ?", (RETRY_INPUTS_MARKER,))
        c.commit()
        c.close()
        self.scan()
        self.assertNotIn('"id"', self.after()["calls"]["msg-bf_keep-3"])  # another command's hash: not this call

    def test_path_keyed_calls_without_ids_get_them_back_and_their_loop_is_found(self):
        self.scan()
        self.assertTrue([f for f in waste.report(self.db, session_id="bf_path", min_share=0, min_usd=0)
                         if f["cause"] == "retry_loop"])

    def test_db_that_finished_the_older_backfill_still_gets_path_ids(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('retry_inputs_backfill_v2_done_claude', '1')")
        c.commit()
        c.close()
        self.scan()
        self.assertIn('"id"', self.after()["calls"]["msg-bf_path-3"])

    def test_db_missing_only_path_call_ids_is_not_marked_done_unread(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("UPDATE tool_results SET is_error = 0 WHERE is_error IS NULL")
        c.execute("DELETE FROM turns WHERE tool_calls LIKE '%\"Bash\"%'")
        c.commit()
        c.close()
        self.scan()
        self.assertIn('"id"', self.after()["calls"]["msg-bf_path-3"])

    def test_call_without_id_beside_one_that_kept_its_id_still_starts_the_backfill(self):
        import json
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("UPDATE tool_results SET is_error = 0 WHERE is_error IS NULL")
        c.execute("DELETE FROM turns WHERE tool_calls LIKE '%\"Bash\"%'")
        raw = c.execute("SELECT tool_calls FROM turns WHERE message_id = 'msg-bf_path-3'").fetchone()[0]
        mixed = json.loads(raw) + [{"name": "Glob", "id": "toolu_kept", "input": {"pattern": "*.py"}}]
        kept = json.dumps([{"name": "Glob", "id": "toolu_kept", "input": {}}])
        c.execute("UPDATE turns SET tool_calls = ? WHERE tool_calls LIKE '%\"name\"%'", (kept,))  # no other row lacks an id
        self.assertTrue(any(not x.get("id") for x in mixed) and any(x.get("id") for x in mixed))
        c.execute("UPDATE turns SET tool_calls = ? WHERE message_id = 'msg-bf_path-3'", (json.dumps(mixed),))
        c.commit()
        from sources.claude import _stores_a_call_without_id
        self.assertTrue(_stores_a_call_without_id(c))
        c.execute("UPDATE turns SET tool_calls = ? WHERE tool_calls LIKE '%\"name\"%'", (kept,))
        self.assertFalse(_stores_a_call_without_id(c))  # every call has its id: nothing to read
        c.rollback()
        c.close()

    def test_path_keyed_call_with_another_input_keeps_no_id(self):
        self.rewrite("msg-bf_path-3", lambda x: {**x, "input": {**x["input"], "file_path": "/src/other.py"}})
        self.scan()
        self.assertNotIn('"id"', self.after()["calls"]["msg-bf_path-3"])

    def test_raw_input_opt_in_leaves_the_backfill_for_a_later_scan(self):
        import sqlite3
        from unittest import mock
        from sources.claude import RETRY_INPUTS_MARKER
        with mock.patch.dict("os.environ", {policy.RAW_ENV: "1"}):
            self.scan()
        c = sqlite3.connect(self.db)
        marker = c.execute("SELECT value FROM schema_meta WHERE key = ?", (RETRY_INPUTS_MARKER,)).fetchone()
        c.close()
        self.assertIsNone(marker)  # not "done": the hashes are still missing
        self.scan()
        self.assertIn(policy.command_hash("pytest -q"), self.after()["calls"]["msg-bf_keep-4"])

    def test_second_run_changes_nothing(self):
        self.scan()
        first = self.after()
        import sqlite3
        from sources.claude import RETRY_INPUTS_MARKER
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM schema_meta WHERE key = ?", (RETRY_INPUTS_MARKER,))  # even forced to repeat
        c.commit()
        c.close()
        self.scan()
        self.assertEqual(self.after(), first)

    def test_measurement_counts_the_sessions_left_unmeasured(self):
        cell = waste.measurement(self.db)["causes"]["retry_loop"]["claude"]
        self.assertIn("next `whytokens scan`", cell["reason"])  # before the backfill ran
        self.scan()
        cell = waste.measurement(self.db)["causes"]["retry_loop"]["claude"]
        self.assertEqual((cell["status"], cell["measured"], cell["unmeasured"]), ("partial", 2, 1))
        self.assertIn("no longer on disk", cell["reason"])
        self.assertTrue([f for f in waste.report(self.db, session_id="bf_keep", min_share=0, min_usd=0)
                         if f["cause"] == "retry_loop"])


if __name__ == "__main__":
    unittest.main()
