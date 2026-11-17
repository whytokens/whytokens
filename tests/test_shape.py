"""Work shape per session, peer baselines, the by-shape table, and the shape mix next to a
verdict. Fixtures are DB rows written directly (tool_calls JSON, prompt_seq, output tokens)."""

import builtins
import contextlib
import http.client
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import api
import budget
import cli
import fixes
import shape
from scanner import get_db, init_db

OPUS = "claude-opus-5-5"   # 50_000 output tokens cost exactly $1.00
GOLDEN = Path(__file__).parent / "fixtures" / "verify_golden.json"


def calls(**kw):
    """calls(Edit=6, Read=2) -> ['Edit']*6 + ['Read']*2"""
    return [n for n, k in kw.items() for _ in range(k)]


def build(specs):
    """specs: dicts id, project, day, calls (tool names), usd, prompts (None = unknown). -> (db path, tmpdir)"""
    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "usage.db"
    conn = get_db(db)
    init_db(conn)
    for sp in specs:
        names, prompts = sp.get("calls", []), sp.get("prompts", 1)
        n = max(len(names), prompts or 0, 1)
        day = sp.get("day", "2026-10-01")
        conn.execute("INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp, turn_count, agent, model)"
                     " VALUES (?,?,?,?,?,?,?)", (sp["id"], sp.get("project", "acme"), day + "T10:00:00Z", day + "T11:00:00Z", n,
                                                  sp.get("agent", "claude"), OPUS))
        for i in range(n):
            tc = json.dumps([{"name": names[i], "input": {}}]) if i < len(names) else None
            conn.execute("INSERT INTO turns (session_id, timestamp, model, output_tokens, tool_calls, tool_name, message_id, prompt_seq)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (sp["id"], day + "T10:00:00Z", OPUS, round(sp.get("usd", 1.0) * 50_000) if i == 0 else 0, tc,
                          names[i] if i < len(names) else None, f"{sp['id']}-{i}", None if prompts is None else min(prompts, i + 1)))
    conn.commit()
    conn.close()
    return db, tmp


class Base(unittest.TestCase):
    def make(self, specs, sync=True):
        db, tmp = build(specs)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        if sync:
            conn = get_db(db)
            shape.sync_shapes(conn)
            conn.close()
        return db

    def shapes(self, db):
        conn = sqlite3.connect(db)
        try:
            return dict(conn.execute("SELECT session_id, shape FROM sessions"))
        finally:
            conn.close()


class DeriveTests(unittest.TestCase):
    def test_one_session_per_shape(self):
        cases = {
            "edit": calls(Edit=4, Write=2, Read=2),
            "read_search": calls(Read=4, Grep=3, Glob=1),
            "shell": calls(Bash=5, PowerShell=2, Read=1),
            "web": calls(WebFetch=4, WebSearch=2, Read=1),
            "connector": calls(mcp__slack__post=4, mcp__slack__read=1, mcp__drive__get=1, Read=1),
            "subagent": calls(Agent=3, Task=2, Read=1),
        }
        for want, names in cases.items():
            self.assertEqual(shape.derive(names)[0], want, want)

    def test_connector_keeps_top_server(self):
        self.assertEqual(shape.derive(calls(mcp__slack__post=4, mcp__drive__get=2, Read=1)), ("connector", "slack"))
        self.assertEqual(shape.derive(calls(Edit=6, Read=1)), ("edit", None))

    def test_threshold_edge(self):
        at = calls(Edit=4, Read=3, Bash=3)        # 4 of 10 = 40%: passes
        below = calls(Edit=4, Read=3, Bash=3) + calls(Grep=1)  # 4 of 11 = 36%: mixed
        self.assertEqual(shape.derive(at)[0], "edit")
        self.assertEqual(shape.derive(below)[0], "mixed")
        self.assertEqual(shape.DOMINANT_SHARE, 0.40)

    def test_mixed_and_chat(self):
        self.assertEqual(shape.derive(calls(Edit=2, Read=2, Bash=2, WebFetch=2))[0], "mixed")
        self.assertEqual(shape.derive(calls(Edit=4))[0], "chat")           # one under the minimum
        self.assertEqual(shape.derive(calls(Edit=5))[0], "edit")           # at the minimum
        self.assertEqual(shape.derive([])[0], "chat")

    def test_uncategorised_tools_count_in_the_denominator(self):
        self.assertEqual(shape.derive(calls(Edit=3, TodoWrite=3, Skill=3))[0], "mixed")  # 3 of 9 = 33%

    def test_codex_tool_names(self):
        self.assertEqual(shape.derive(calls(exec_command=5, apply_patch=1))[0], "shell")
        self.assertEqual(shape.derive(calls(apply_patch=5, exec_command=1))[0], "edit")

    def test_labels_cover_every_shape(self):
        for s in (*shape.CATEGORIES, "mixed", "chat"):
            self.assertIn(s, shape.LABELS)


class SyncTests(Base):
    def test_sync_fills_and_is_idempotent(self):
        db = self.make([{"id": "e", "calls": calls(Edit=5, Read=1), "prompts": 2},
                        {"id": "c", "calls": calls(mcp__x__y=5), "prompts": 1},
                        {"id": "q", "calls": calls(Read=2), "prompts": 1}], sync=False)
        conn = get_db(db)
        self.assertEqual(shape.sync_shapes(conn), 3)
        self.assertEqual(shape.sync_shapes(conn), 0)
        row = conn.execute("SELECT shape, shape_detail, shape_version FROM sessions WHERE session_id = 'c'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("connector", "x", shape.SHAPE_VERSION))
        self.assertEqual(self.shapes(db), {"e": "edit", "c": "connector", "q": "chat"})

    def test_new_turns_or_new_version_rederive(self):
        db = self.make([{"id": "s", "calls": calls(Edit=3), "prompts": 1}])
        self.assertEqual(self.shapes(db)["s"], "chat")
        conn = get_db(db)
        for i in range(3):
            conn.execute("INSERT INTO turns (session_id, timestamp, model, tool_calls, message_id) VALUES ('s','2026-10-01T10:00:00Z',?,?,?)",
                         (OPUS, json.dumps([{"name": "Edit"}]), f"s-more{i}"))
        conn.execute("UPDATE sessions SET turn_count = turn_count + 3 WHERE session_id = 's'")
        self.assertEqual(shape.sync_shapes(conn), 1)
        self.assertEqual(conn.execute("SELECT shape FROM sessions").fetchone()[0], "edit")
        conn.execute("UPDATE sessions SET shape_version = 0")
        self.assertEqual(shape.sync_shapes(conn), 1)
        conn.close()

    def test_tool_call_backfill_without_new_turns_rederives(self):
        db = self.make([{"id": "s", "calls": calls(Edit=1), "prompts": 1}])
        self.assertEqual(self.shapes(db)["s"], "chat")
        conn = get_db(db)
        conn.execute("UPDATE turns SET tool_calls = ? WHERE session_id = 's'", (json.dumps([{"name": "Bash"}] * 6),))
        self.assertEqual(shape.sync_shapes(conn), 1)
        self.assertEqual(conn.execute("SELECT shape FROM sessions").fetchone()[0], "shell")
        conn.close()

    def test_backfill_reads_only_the_database(self):
        db = self.make([{"id": "s", "calls": calls(Bash=6), "prompts": 1}], sync=False)
        conn = get_db(db)
        real_open = builtins.open
        with mock.patch.object(builtins, "open", side_effect=AssertionError("backfill must not open files")), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("no transcript reads")), \
             mock.patch.object(Path, "open", side_effect=AssertionError("no transcript reads")):
            shape.sync_shapes(conn)
        conn.close()
        self.assertIs(builtins.open, real_open)
        self.assertEqual(self.shapes(db)["s"], "shell")


def peer_specs(prefix, shape_calls, costs, project="acme", prompts=1):
    return [{"id": f"{prefix}{i}", "project": project, "calls": shape_calls, "usd": c, "prompts": prompts}
            for i, c in enumerate(costs)]


EDIT = calls(Edit=6)
SHELL = calls(Bash=6)


class PeerTests(Base):
    def peers(self, specs):
        db = self.make(specs)
        return shape.peers(shape.session_rows(db))

    def test_median_not_mean(self):
        # peers 1,1,1,1,1,10: the mean is 2.5 (so 2.2 would read 0.9x); the median is 1 (2.2x)
        p = self.peers(peer_specs("p", EDIT, [1, 1, 1, 1, 1, 10]) + peer_specs("t", EDIT, [2.2]))
        self.assertAlmostEqual(p["t0"]["median"], 1.0)
        self.assertAlmostEqual(p["t0"]["x"], 2.2)
        self.assertTrue(shape.is_outlier(p["t0"]["x"]))

    def test_outlier_edge(self):
        base = peer_specs("p", EDIT, [1.0] * 6)
        high = self.peers(base + peer_specs("t", EDIT, [2.0]))["t0"]["x"]
        low = self.peers(base + peer_specs("t", EDIT, [1.9]))["t0"]["x"]
        self.assertTrue(shape.is_outlier(high))     # exactly 2.0x
        self.assertFalse(shape.is_outlier(low))     # 1.9x
        self.assertFalse(shape.is_outlier(None))

    def test_uses_shape_group_when_big_enough(self):
        p = self.peers(peer_specs("e", EDIT, [1] * 5) + peer_specs("s", SHELL, [4] * 5))
        self.assertEqual((p["e0"]["basis"], p["e0"]["n"], p["e0"]["x"]), ("shape", 5, 1.0))
        self.assertEqual((p["s0"]["basis"], p["s0"]["n"]), ("shape", 5))

    def test_small_group_falls_back_to_project_and_says_so(self):
        p = self.peers(peer_specs("e", EDIT, [1] * 4) + peer_specs("s", SHELL, [3] * 4))
        self.assertEqual((p["e0"]["basis"], p["e0"]["n"]), ("project", 8))
        self.assertAlmostEqual(p["e0"]["median"], 2.0)   # median of 1,1,1,1,3,3,3,3
        self.assertIn("sessions of any shape", shape.baseline_text(p["e0"]))

    def test_no_baseline_means_no_multiple(self):
        p = self.peers(peer_specs("e", EDIT, [1] * 3))
        self.assertIsNone(p["e0"]["x"])
        self.assertEqual(shape.baseline_text(p["e0"]), "")

    def test_other_projects_are_not_peers(self):
        p = self.peers(peer_specs("a", EDIT, [1] * 5, project="one") + peer_specs("b", EDIT, [9] * 2, project="two"))
        self.assertIsNone(p["b0"]["x"])
        self.assertAlmostEqual(p["a0"]["x"], 1.0)

    def test_unknown_prompts_are_excluded(self):
        specs = peer_specs("p", EDIT, [1] * 5) + [{"id": "u", "calls": EDIT, "usd": 50, "prompts": None}]
        p = self.peers(specs)
        self.assertNotIn("u", p)
        self.assertEqual(p["p0"]["n"], 5)
        self.assertAlmostEqual(p["p0"]["median"], 1.0)

    def test_cost_per_prompt_not_per_session(self):
        specs = peer_specs("p", EDIT, [1] * 5) + peer_specs("many", EDIT, [4], prompts=4)  # $4 over 4 prompts = $1/prompt
        self.assertAlmostEqual(self.peers(specs)["many0"]["x"], 1.0)

    def test_baseline_text_names_the_baseline(self):
        p = self.peers(peer_specs("p", EDIT, [1] * 17) + peer_specs("t", EDIT, [2.4]))
        self.assertEqual(shape.baseline_text(p["t0"]), "2.4x the median for edit-heavy sessions in this project, n=18")


class BreakdownTests(Base):
    def test_rows_sum_to_total_and_observations_are_not_added(self):
        db = self.make(peer_specs("e", EDIT, [1] * 5 + [10]) + peer_specs("s", SHELL, [2, 2]))
        rows = shape.session_rows(db)
        findings = [{"session_id": "e0", "usd": 0.5, "in_total": True},
                    {"session_id": "e1", "usd": 9.0, "in_total": False},   # observation tier
                    {"session_id": "s0", "usd": 0.25}]
        table = shape.breakdown(rows, findings)
        total = sum(r["usd"] for r in rows)
        self.assertAlmostEqual(sum(r["usd"] for r in table), total)
        self.assertAlmostEqual(sum(r["share"] for r in table), 1.0)
        by = {r["shape"]: r for r in table}
        self.assertAlmostEqual(by["edit"]["overhead_usd"], 0.5)   # the observation finding is not summed
        self.assertAlmostEqual(by["shell"]["overhead_usd"], 0.25)
        self.assertEqual(by["edit"]["label"], "edit-heavy")
        self.assertIn("edit-heavy", shape.format_breakdown(table))

    def test_outlier_count_is_a_count_not_a_cost(self):
        db = self.make(peer_specs("e", EDIT, [1] * 6 + [3]))
        out = api.get_shapes(db)
        self.assertEqual(out["outliers"], 1)
        self.assertAlmostEqual(sum(r["usd"] for r in out["rows"]), 9.0)   # 6 x $1 + $3: the outlier is in it once
        self.assertNotIn("outlier_usd", out)


FIX_DAY = date(2026, 9, 1)


def sides(before, after, project="acme"):
    """before/after: lists of call-lists, one session per day around the fix day."""
    specs = []
    for i, c in enumerate(before):
        specs.append({"id": f"b{i}", "project": project, "day": (FIX_DAY - timedelta(days=len(before) - i)).isoformat(), "calls": c, "usd": 1.0})
    for i, c in enumerate(after):
        specs.append({"id": f"a{i}", "project": project, "day": (FIX_DAY + timedelta(days=i)).isoformat(), "calls": c, "usd": 1.0})
    return specs


class MixTests(Base):
    def view(self, before, after):
        db = self.make(sides(before, after))
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        fixes.record_fix(conn, "acme", "idle_gap", "n", FIX_DAY.isoformat())
        fix = fixes.list_fixes(conn)[0]
        conn.close()
        return db, fix, fixes.change_view(db, fix, today=date(2026, 12, 1))

    def mix(self, before, after):
        return self.view(before, after)[2]

    def test_warning_fires_past_the_threshold_and_names_the_shift(self):
        m = self.mix([EDIT] * 8 + [SHELL] * 4, [EDIT] * 4 + [SHELL] * 8)["mix"]   # edit 67% -> 33%
        self.assertTrue(m["warn"])
        self.assertIn("the work changed", m["warning"])
        self.assertIn("edit-heavy 67% -> 33%", m["warning"])
        self.assertIn("read this verdict with care", m["warning"])

    def test_quiet_below_the_threshold(self):
        m = self.mix([EDIT] * 7 + [SHELL] * 5, [EDIT] * 6 + [SHELL] * 6)["mix"]   # edit 58% -> 50%
        self.assertFalse(m["warn"])
        self.assertEqual(m["warning"], "")

    def test_edge_exactly_at_threshold_warns_and_just_under_does_not(self):
        self.assertEqual(shape.MIX_SHIFT, 0.20)
        at = self.mix([EDIT] * 10, [EDIT] * 8 + [SHELL] * 2)       # edit 100% -> 80%: exactly 20 points
        under = self.mix([EDIT] * 10, [EDIT] * 9 + [SHELL])        # 100% -> 90%
        self.assertTrue(at["mix"]["warn"])
        self.assertFalse(under["mix"]["warn"])

    def test_no_warning_while_waiting(self):
        v = self.mix([EDIT] * 12, [SHELL] * 3)
        self.assertEqual(v["state"], "waiting")
        self.assertFalse(v["mix"]["warn"])
        self.assertTrue(v["mix"]["after"])      # the mix itself is still shown

    def test_same_shape_line_needs_min_sessions_on_both_sides(self):
        m = self.mix([EDIT] * 10 + [SHELL] * 2, [EDIT] * 10 + [SHELL] * 2)["mix"]
        self.assertEqual([s["shape"] for s in m["same_shape"]], ["edit"])
        s = m["same_shape"][0]
        self.assertEqual((s["before"]["sessions"], s["after"]["sessions"]), (10, 10))
        m = self.mix([EDIT] * 9 + [SHELL] * 3, [EDIT] * 10 + [SHELL] * 2)["mix"]
        self.assertEqual(m["same_shape"], [])

    def test_shape_never_changes_the_verdict(self):
        same = [EDIT] * 12
        calm = self.mix(same, same)
        db, fix, shifted = self.view(same, [SHELL] * 12)
        plain = fixes.verify(db, fix)
        self.assertEqual(shifted["status"], plain["status"])
        self.assertEqual({k: shifted[k] for k in ("before", "after", "saving_usd")},
                         {k: plain[k] for k in ("before", "after", "saving_usd")})
        self.assertEqual(calm["status"], "no material change")


class VerdictUnchangedTests(unittest.TestCase):
    def test_verify_matches_the_frozen_baseline(self):
        from tests.verify_golden import capture
        self.assertEqual(capture(), json.loads(GOLDEN.read_text(encoding="utf-8")))


class TagTests(Base):
    def test_tags_and_budget_do_not_clobber_each_other(self):
        db = self.make([{"id": "s", "calls": EDIT, "prompts": 1}])
        budget.set_tag("acme", "work / non-coding", db)
        budget.set_budget(100, db)
        self.assertEqual((budget.load_budget(db), budget.load_tags(db)), (100.0, {"acme": "work / non-coding"}))
        budget.set_budget(None, db)
        self.assertEqual((budget.load_budget(db), budget.load_tags(db)), (None, {"acme": "work / non-coding"}))
        budget.set_budget(50, db)
        budget.set_tag("acme", "  ", db)       # blank clears
        self.assertEqual((budget.load_budget(db), budget.load_tags(db)), (50.0, {}))
        budget.set_budget(None, db)
        self.assertFalse(budget.budget_path(db).exists())   # nothing left set: no file

    def test_bad_tags_are_rejected(self):
        db = self.make([{"id": "s", "calls": EDIT, "prompts": 1}])
        for bad in ("x" * 41, "a\nb", 5):
            with self.assertRaises(ValueError):
                budget.set_tag("acme", bad, db)
        self.assertEqual(budget.load_tags(db), {})


class TagLockTests(Base):
    def test_stale_lock_file_is_cleared_and_fresh_one_blocks(self):
        import os, time
        db = self.make([{"id": "s", "calls": EDIT, "prompts": 1}])
        lock = budget.budget_path(db).with_name(budget.budget_path(db).name + ".lock")
        lock.write_text("")
        os.utime(lock, (time.time() - 60, time.time() - 60))
        budget.set_tag("acme", "x", db)                      # stale: taken over
        self.assertFalse(lock.exists())
        lock.write_text("")                                  # fresh: another process holds it
        with mock.patch.object(budget.time, "time", side_effect=[0, 0, 0, 99, 99, 99]),                 mock.patch.object(budget.time, "sleep"), self.assertRaises(OSError):
            budget.set_tag("acme", "y", db)
        self.assertEqual(budget.load_tags(db), {"acme": "x"})


class ApiTests(Base):
    TOKEN = "tok-shape"

    def setUp(self):
        self.db = self.make(peer_specs("e", EDIT, [1] * 6 + [3]))
        for p in (mock.patch.object(api, "DB_PATH", Path(self.db)), mock.patch.object(api.DashboardHandler, "token", self.TOKEN),
                  mock.patch.object(api.DashboardHandler, "allow_any_host", False)):
            p.start()
            self.addCleanup(p.stop)
        self.server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        h = {"Content-Type": "application/json"}
        if token:
            h[api.TOKEN_HEADER] = token
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def test_tag_field_is_required(self):
        api.post_project_tag({"project": "acme", "tag": "client"}, self.db)
        with self.assertRaises(ValueError):
            api.post_project_tag({"project": "acme"}, self.db)
        self.assertEqual(budget.load_tags(self.db), {"acme": "client"})

    def test_shapes_endpoint(self):
        status, raw = self.call("GET", "/api/shapes")
        d = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(d["outliers"], 1)
        self.assertEqual(d["sessions"]["e6"]["shape"], "edit")
        self.assertAlmostEqual(d["sessions"]["e6"]["x"], 3.0)
        self.assertEqual(d["rows"][0]["label"], "edit-heavy")

    def test_tag_post_needs_the_token_and_a_known_project(self):
        body = {"project": "acme", "tag": "work"}
        self.assertEqual(self.call("POST", "/api/project-tag", body)[0], 403)
        self.assertEqual(self.call("POST", "/api/project-tag", {"project": "ghost", "tag": "x"}, self.TOKEN)[0], 400)
        self.assertEqual(self.call("POST", "/api/project-tag", {"project": "acme", "tag": "y" * 99}, self.TOKEN)[0], 400)
        self.assertEqual(self.call("POST", "/api/project-tag", [1], self.TOKEN)[0], 400)
        status, raw = self.call("POST", "/api/project-tag", body, self.TOKEN)
        self.assertEqual((status, json.loads(raw)["tag"]), (200, "work"))
        self.assertEqual(json.loads(self.call("GET", "/api/shapes")[1])["tags"], {"acme": "work"})
        self.call("POST", "/api/project-tag", {"project": "acme", "tag": None}, self.TOKEN)
        self.assertEqual(json.loads(self.call("GET", "/api/shapes")[1])["tags"], {})


class CliTests(Base):
    def run_cli(self, db, fn, *args):
        out = io.StringIO()
        with mock.patch.object(cli, "DB_PATH", Path(db)), contextlib.redirect_stdout(out):
            try:
                fn(*args)
            except SystemExit as e:
                out.write(f"[exit {e.code}]")
        return out.getvalue()

    def test_shapes_table_and_project_tag(self):
        db = self.make(peer_specs("e", EDIT, [1] * 6 + [3]))
        text = self.run_cli(db, cli.cmd_shapes)
        self.assertIn("edit-heavy", text)
        self.assertIn("1 session cost 2x or more their peers", text)
        self.assertIn("acme: no tag", self.run_cli(db, cli.cmd_project, ["tag", "acme"]))
        self.assertIn("acme: work", self.run_cli(db, cli.cmd_project, ["tag", "acme", "work"]))
        self.assertIn("acme: no tag", self.run_cli(db, cli.cmd_project, ["tag", "acme", "--clear"]))
        self.assertIn("[exit 1]", self.run_cli(db, cli.cmd_project, ["tag", "ghost", "x"]))
        self.assertIn("[exit 2]", self.run_cli(db, cli.cmd_project, []))


if __name__ == "__main__":
    unittest.main()
