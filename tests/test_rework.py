"""Rework signals D1-D5 (rework.py) and the blind labelling tool: measurements only.

A known-good session scores zero on every signal; one mutant session per defect a signal claims
to detect (injected errors, re-edits, interrupts, follow-ups, a quick restart) scores high on that
signal. scripts/falsify_rework.py breaks the scorer one rule at a time and checks this goes red.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import rework
from sources import ClaudeSource, scan_all
from tests.test_waste import T0, _rec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import label_rework  # noqa: E402

CWD = "/home/dev/acme/app"


def _at(m):
    return (T0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(sid, m, content):
    return json.dumps({"type": "user", "sessionId": sid, "timestamp": _at(m), "cwd": CWD,
                       "message": {"role": "user", "content": content}})


def _result(sid, n, err):
    return json.dumps({"type": "user", "sessionId": sid, "timestamp": "", "cwd": CWD,
                       "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu-{sid}-{n}-0",
                                                                "content": "", "is_error": err}]}})


def _limit(sid, m):
    return json.dumps({"type": "assistant", "sessionId": sid, "timestamp": _at(m), "cwd": CWD, "error": "rate_limit",
                       "isApiErrorMessage": True, "message": {"model": "<synthetic>", "role": "assistant",
                       "content": [{"type": "text", "text": "API Error: Server is temporarily limiting requests"}],
                       "usage": {"input_tokens": 0, "output_tokens": 0}}})


def _session(sid, start, prompts):
    """prompts: [(gap_min, [(tool, arg, failed)], interrupted)]."""
    lines, t, n = [], start, 0
    for gap, tools, interrupted in prompts:
        t += gap
        lines.append(_user(sid, t, f"step {n}"))
        for tool, arg, failed in tools:
            n += 1
            t += 1
            lines += [_rec(sid, n, _at(t), tools=[(tool, arg)]).split("\n")[0], _result(sid, n, failed)]
        if interrupted:
            lines.append(_user(sid, t, "[Request interrupted by user for tool use]"))
    n += 1
    lines.append(_rec(sid, n, _at(t + 1)))
    return lines


def _edit(path, failed=False):
    return ("Edit", {"file_path": path, "old_string": "a", "new_string": "b"}, failed)


def _read(path):
    return ("Read", {"file_path": path}, False)


def _bash(failed):
    return ("Bash", {"command": "pytest -q"}, failed)


DAY = 1440
GOOD = [(0, [_read("/s/a1.py"), _edit("/s/a1.py"), _edit("/s/a1.py")], False),  # a same-prompt re-edit is not churn
        (40, [_read("/s/a2.py"), _edit("/s/a2.py"), _bash(False)], False),
        (40, [_edit("/s/a3.py")], False)]
FIXTURES = {
    "good": _session("good", 0, GOOD),
    "errors": _session("errors", DAY, [(0, [_bash(True), _bash(True)], False), (40, [_bash(True)], False), (40, [_bash(False)], False)]),
    "churn": _session("churn", 2 * DAY, [(0, [_edit("/s/x.py")], False), (40, [_edit("/s/x.py")], False), (40, [_edit("/s/x.py")], False)]),
    "interrupt": _session("interrupt", 3 * DAY, [(0, [_bash(False)], True), (40, [_bash(False)], True), (40, [_bash(False)], False)]),
    "followups": _session("followups", 4 * DAY, [(0, [_bash(False)], False), (2, [_bash(False)], False),
                                                 (2, [_bash(False)], False), (2, [_bash(False)], False)]),
    "short": _session("short", 5 * DAY, [(0, [_bash(False)], False), (40, [_bash(False)], False)]),
    "next": _session("next", 5 * DAY + 50, [(0, [_bash(False)], False), (40, [_bash(False)], False),
                                            (40, [_bash(False)], False), (40, [_bash(False)], False)]),
    "after": _session("after", 5 * DAY + 180, [(0, [_bash(False)], False), (40, [_bash(False)], False)]),  # 5 min after "next" (4 prompts: not short)
    "limit": _session("limit", 6 * DAY, GOOD) + [_limit("limit", 6 * DAY + 200)],
    "gone": _session("gone", 7 * DAY, GOOD),
    # two prompts before one reply, then a last prompt nobody answered: 3 prompts, one request
    "double": [_user("double", 8 * DAY, "a"), _user("double", 8 * DAY + 10, "b"),
               _rec("double", 1, _at(8 * DAY + 11)), _user("double", 8 * DAY + 20, "c")],
    # the second prompt comes 10 min after the first; its reply only 35 min later
    "slow": [_user("slow", 9 * DAY, "a"), _rec("slow", 1, _at(9 * DAY + 1)), _user("slow", 9 * DAY + 10, "b"),
             _rec("slow", 2, _at(9 * DAY + 45))],
    # A runs 0-10, B (inside A) 5-6, C starts 15: C is A's restart, 5 min after A ends
    "ovA": [_user("ovA", 10 * DAY, "a"), _rec("ovA", 1, _at(10 * DAY + 1)), _user("ovA", 10 * DAY + 8, "b"),
            _rec("ovA", 2, _at(10 * DAY + 10))],
    "ovB": [_user("ovB", 10 * DAY + 5, "a"), _rec("ovB", 1, _at(10 * DAY + 5)), _rec("ovB", 2, _at(10 * DAY + 6))],
    "ovC": [_user("ovC", 10 * DAY + 15, "a"), _rec("ovC", 1, _at(10 * DAY + 16))],
}


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
        (proj / "gone.jsonl").unlink()  # pruned after it was scanned
        cls.m = rework.measure(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestSignals(_Scan):
    def test_known_good_scores_zero_everywhere(self):
        g = self.m["good"]
        self.assertEqual(g["prompts"], 3)
        self.assertEqual({s: g[s] for s in rework.SIGNALS}, dict.fromkeys(rework.SIGNALS, 0))
        self.assertIs(g["limit_hit"], False)

    def test_each_mutant_scores_on_its_own_signal(self):
        self.assertAlmostEqual(self.m["errors"]["error_rate"], 3 / 3)
        self.assertAlmostEqual(self.m["churn"]["churn_rate"], 2 / 3)
        self.assertAlmostEqual(self.m["interrupt"]["interrupt_rate"], 2 / 3)
        self.assertAlmostEqual(self.m["followups"]["followups"], 3)  # 4 prompts, one request
        self.assertEqual((self.m["short"]["restarted"], self.m["next"]["restarted"], self.m["after"]["restarted"]), (1, 0, 0))
        for sid in ("errors", "churn", "interrupt", "followups"):
            others = [s for s in rework.SIGNALS if self.m[sid][s] and s != {"errors": "error_rate", "churn": "churn_rate",
                      "interrupt": "interrupt_rate", "followups": "followups"}[sid]]
            self.assertEqual(others, [], sid)

    def test_prompts_and_their_times_come_from_the_prompt_records(self):
        self.assertEqual((self.m["double"]["prompts"], self.m["double"]["followups"]), (3, 2))
        self.assertEqual(self.m["slow"]["followups"], 1)
        self.assertEqual(self.m["ovA"]["restarted"], 1)

    def test_missing_result_rows_leave_d1_unmeasured(self):
        db = str(Path(self.tmp) / "missing.db")
        shutil.copy(self.db, db)
        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM tool_results WHERE rowid IN (SELECT rowid FROM tool_results WHERE session_id = 'errors' LIMIT 1)")
        self.assertIsNone(rework.measure(db, read_transcripts=False)["errors"]["error_rate"])

    def _copy(self, name):
        db = str(Path(self.tmp) / name)
        shutil.copy(self.db, db)
        return db

    def test_calls_without_ids_leave_d1_unmeasured(self):
        db = self._copy("noid.db")
        with sqlite3.connect(db) as c:
            for rowid, blob in c.execute("SELECT rowid, tool_calls FROM turns WHERE session_id = 'good'").fetchall():
                calls = [{k: v for k, v in x.items() if k != "id"} for x in json.loads(blob or "[]")]
                c.execute("UPDATE turns SET tool_calls = ? WHERE rowid = ?", (json.dumps(calls), rowid))
            c.execute("DELETE FROM tool_results WHERE session_id = 'good'")
        self.assertIsNone(rework.measure(db)["good"]["error_rate"])

    def test_edits_without_a_prompt_ordinal_leave_d2_unmeasured(self):
        db = self._copy("noseq.db")
        with sqlite3.connect(db) as c:
            c.execute("UPDATE turns SET prompt_seq = NULL WHERE session_id = 'churn'")
        self.assertIsNone(rework.measure(db)["churn"]["churn_rate"])

    def test_every_copy_is_read_and_a_limit_stop_in_any_counts(self):
        db = self._copy("copies.db")
        full = rework._transcript(sqlite3.connect(db), "limit")
        part = Path(self.tmp) / "branch" / "limit.jsonl"  # an incomplete copy, bigger on disk
        part.parent.mkdir(exist_ok=True)
        lines = full.read_text(encoding="utf-8").splitlines()
        part.write_text("\n".join(lines[:2] + ["x" * (len(full.read_bytes()) + 10)]) + "\n", encoding="utf-8")
        with sqlite3.connect(db) as c:
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, 0, 0, 'claude')", (str(part),))
        # and a shorter copy of "good" that ends on a limit stop the longer copy never reached
        short = Path(self.tmp) / "branch" / "good.jsonl"
        good = rework._transcript(sqlite3.connect(db), "good").read_text(encoding="utf-8").splitlines()
        short.write_text("\n".join(good[:2] + [_limit("good", 1)]) + "\n", encoding="utf-8")
        with sqlite3.connect(db) as c:
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, 0, 0, 'claude')", (str(short),))
        got = rework.measure(db)
        self.assertEqual((got["limit"]["limit_hit"], got["limit"]["prompts"]), (True, 3))
        self.assertEqual(got["limit"]["interrupt_rate"], 0)  # measured from the complete copy, not the bigger one
        self.assertEqual((got["good"]["limit_hit"], got["good"]["prompts"]), (True, 3))

    def test_only_a_shorter_copy_left_is_unknown_not_clean(self):
        db = self._copy("pruned.db")
        full = rework._transcript(sqlite3.connect(db), "limit")
        lines = full.read_text(encoding="utf-8").splitlines()
        part = Path(self.tmp) / "pruned" / "limit.jsonl"
        part.parent.mkdir(exist_ok=True)
        part.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")  # 1 of 3 prompts, no stop
        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM processed_files WHERE path = ?", (str(full),))
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, 0, 0, 'claude')", (str(part),))
        m = rework.measure(db)
        self.assertEqual((m["limit"]["limit_hit"], m["limit"]["prompts"], m["limit"]["interrupt_rate"]), (None, 3, None))
        self.assertEqual(rework.separation(m, {"limit": "rework", "good": "fine"})["churn_rate"]["n_rework"], 0)
        part.write_text("\n".join(lines[:2] + [_limit("limit", 1)]) + "\n", encoding="utf-8")  # short, but shows the stop
        self.assertIs(rework.measure(db)["limit"]["limit_hit"], True)

    def test_transcript_lookup_is_exact_and_skips_stale_rows(self):
        db = str(Path(self.tmp) / "stale.db")
        shutil.copy(self.db, db)
        decoy = Path(self.tmp) / "xgood.jsonl"
        decoy.write_text("x" * 100_000, encoding="utf-8")  # bigger than the real one: size alone would pick it
        with sqlite3.connect(db) as c:
            real = c.execute("SELECT path, mtime, lines FROM processed_files WHERE path LIKE '%' || ? ", (chr(92) + "good.jsonl",)).fetchone() \
                or c.execute("SELECT path, mtime, lines FROM processed_files WHERE path LIKE '%/good.jsonl'").fetchone()
            c.execute("DELETE FROM processed_files WHERE path = ?", (real[0],))
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, 0, 0, 'claude')",
                      (str(Path(self.tmp) / "moved" / "good.jsonl"),))
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, 0, 0, 'claude')", (str(decoy),))
            c.execute("INSERT INTO processed_files (path, mtime, lines, source) VALUES (?, ?, ?, 'claude')", real)
        with sqlite3.connect(db) as c:
            self.assertEqual(rework._transcript(c, "good"), Path(real[0]))

    def test_limit_stop_flagged_and_unknown_when_transcript_gone(self):
        self.assertIs(self.m["limit"]["limit_hit"], True)
        self.assertIsNone(self.m["gone"]["limit_hit"])
        self.assertIsNone(self.m["gone"]["interrupt_rate"])  # unmeasured, never zero
        self.assertIsNone(self.m["gone"]["followups"])
        self.assertEqual(self.m["gone"]["prompts"], 3)  # from prompt_seq once the transcript is gone
        self.assertEqual(self.m["gone"]["churn_rate"], 0)  # DB-only signals still measured

    def test_unmeasured_errors_are_none(self):
        db = str(Path(self.tmp) / "copy.db")
        shutil.copy(self.db, db)
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tool_results SET is_error = NULL WHERE session_id = 'errors' AND rowid IN "
                      "(SELECT rowid FROM tool_results WHERE session_id = 'errors' LIMIT 1)")
        self.assertIsNone(rework.measure(db, read_transcripts=False)["errors"]["error_rate"])


class TestScorer(_Scan):
    def test_auc(self):
        self.assertEqual(rework.auc([3, 4], [1, 2]), 1.0)
        self.assertEqual(rework.auc([1, 2], [3, 4]), 0.0)
        self.assertEqual(rework.auc([1, 1], [1, 1]), 0.5)
        self.assertIsNone(rework.auc([], [1]))

    def test_separation_excludes_limit_and_unknown_sessions(self):
        labels = {"errors": "rework", "churn": "rework", "good": "fine", "limit": "rework", "gone": "rework", "next": "fine"}
        r = rework.separation(self.m, labels)
        self.assertEqual((r["error_rate"]["n_fine"], r["error_rate"]["n_rework"]), (2, 2))
        self.assertEqual(r["error_rate"]["auc"], 0.75)  # errors 1.0 > both fine; churn 0 ties both
        self.assertEqual(r["churn_rate"]["auc"], 0.75)
        self.assertEqual(r["error_rate"]["mean_rework"], 0.5)


class TestLabelTool(_Scan):
    def test_pick_is_measurable_unlabelled_and_spread(self):
        got = label_rework.pick(self.m, {"good": "fine"}, 50)
        self.assertNotIn("good", got)
        self.assertNotIn("limit", got)
        self.assertNotIn("gone", got)
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(label_rework.pick(self.m, {}, 3), label_rework.pick(self.m, {}, 3))

    def test_labelling_is_blind_and_saved_locally(self):
        path = Path(self.tmp) / "labels.json"
        answers = iter(["f", "r", "s", "q"])
        shown = []
        orig = label_rework.card
        label_rework.card = lambda *a: shown.append(orig(*a)) or shown[-1]
        try:
            label_rework.main(["--n", "5"], ask=lambda _: next(answers), db_path=self.db, labels_path=path)
        finally:
            label_rework.card = orig
        labels = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(labels.values()), ["fine", "rework"])
        for text in shown:
            for sig in rework.SIGNALS:
                self.assertNotIn(sig, text)
            self.assertIn("first prompt: ", text)
            self.assertNotIn("(not found)", text)  # the transcript prompts are shown for recall



class TestDbPathIsNotParsedAsAUri(unittest.TestCase):
    def test_a_path_with_uri_characters_opens_that_file_read_only(self):
        import shutil
        import sqlite3
        import tempfile
        import scanner
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "data#1 50%.db"
        conn = sqlite3.connect(db)
        scanner.init_db(conn)
        conn.close()
        rework.measure(str(db), read_transcripts=False)  # raised "no such table" on the file "data"
        self.assertEqual(sorted(p.name for p in tmp.iterdir() if not p.name.startswith(db.name)), [])


if __name__ == "__main__":
    unittest.main()
