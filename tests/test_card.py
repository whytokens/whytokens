"""test_card.py - the share card carries aggregates only; leakcheck proves what it leaves out."""

import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import card
import cli
import fixes
from scanner import init_db
from tests import leakcheck
from tests.test_fixes import FIX_DATE, _build

SECRETS = {"project_name": "zq-secret-project", "git_branch": "feature/zq-secret-branch",
           "topic": "zq secret session title", "cwd": "C:/zq/secret/cwd"}


def _corpus(testcase, reduced=True):
    tmp, db = _build([True] * 12, [False] * 12 if reduced else [True] * 12)
    testcase.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("UPDATE sessions SET project_name=?, git_branch=?, topic=?", (SECRETS["project_name"], SECRETS["git_branch"], SECRETS["topic"]))
    conn.execute("UPDATE turns SET cwd=?", (SECRETS["cwd"],))
    conn.commit()
    if reduced:
        fixes.record_fix(conn, SECRETS["project_name"], "resident_output", "zq secret note", FIX_DATE)
    conn.close()
    return db


class CardTests(unittest.TestCase):
    def setUp(self):
        self.db = _corpus(self)
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def make(self, name="c.html", **kw):
        card.write(self.db, self.out / name, **kw)
        return (self.out / name).read_text(encoding="utf-8")

    def test_html_and_svg_leak_nothing(self):
        for name in ("c.html", "c.svg"):
            text = self.make(name)
            self.assertIn("API-equivalent", text)
            self.assertIn("Verified change", text)
            leakcheck.assert_no_leak(self, self.db, text)
            self.assertEqual(leakcheck.leaked(self.db, text), [])

    def test_leakcheck_sees_a_leak(self):  # the guard is falsified: a mutant that prints a project name goes red
        text = self.make()
        for key, value in SECRETS.items():
            self.assertEqual(leakcheck.leaked(self.db, text + value), [value], key)
        real = card._lines
        with mock.patch.object(card, "_lines", lambda m: [*real(m), ("row", SECRETS["project_name"])]):
            mutant = card.render_html(card.build(self.db))
        self.assertEqual(leakcheck.leaked(self.db, mutant), [SECRETS["project_name"]])

    def test_project_tag_only_behind_opt_in(self):
        import budget
        budget.set_tag(SECRETS["project_name"], "Team Alpha", self.db)
        self.assertNotIn("Team Alpha", self.make())
        text = self.make(include_projects=True)
        self.assertIn("Team Alpha", text)  # the display tag, never the internal name
        self.assertNotIn(SECRETS["project_name"], text)
        self.assertEqual(leakcheck.leaked(self.db, text, ignore=["Team Alpha"]), [])

    def test_untagged_project_is_not_named_even_with_opt_in(self):
        self.assertNotIn(SECRETS["project_name"], self.make(include_projects=True))

    def test_malformed_timestamp_never_reaches_the_card(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE turns SET timestamp='C:/zq/secret/dir' WHERE id=(SELECT MIN(id) FROM turns)")
        conn.commit()
        conn.close()
        self.assertNotIn("C:/zq", self.make())

    def test_refuses_to_overwrite_the_database(self):
        dbhtml = Path(self.db).with_suffix(".html")
        Path(self.db).rename(dbhtml)
        with self.assertRaises(ValueError):
            card.write(dbhtml, dbhtml)
        self.assertTrue(sqlite3.connect(dbhtml).execute("SELECT COUNT(*) FROM turns").fetchone()[0])

    def test_leakcheck_sees_json_and_escaped_and_short_values(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE turns SET tool_calls=?, tool_name=? WHERE id=(SELECT MIN(id) FROM turns)",
                     ('[{"name":"Bash","input":{"command":"rm zqsecretcmd"}}]', "mcp__zqsrv__tool"))
        conn.execute("UPDATE sessions SET git_branch='Q&Ops'")
        conn.commit()
        conn.close()
        for needle in ("rm zqsecretcmd", "mcp__zqsrv__tool", "Q&Ops"):
            self.assertIn(needle, leakcheck.leaked(self.db, "x " + needle.replace("&", "&amp;")), needle)

    def test_leakcheck_sees_free_text_in_an_extension_key_value_table(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE ext_notes (key TEXT, value TEXT)")
        conn.execute("INSERT INTO ext_notes VALUES ('k', 'zqprivateproject')")
        conn.commit()
        conn.close()
        self.assertEqual(leakcheck.leaked(self.db, "x zqprivateproject"), ["zqprivateproject"])

    def test_leakcheck_sees_status_column_and_schema_meta_json_keys(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE ext_s (status TEXT)")
        conn.execute("INSERT INTO ext_s VALUES ('zqstatusfree')")
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('m', '{\"C:/zqpathkey\": 1}')")
        conn.commit()
        conn.close()
        self.assertEqual(leakcheck.leaked(self.db, "zqstatusfree C:/zqpathkey"), ["C:/zqpathkey", "zqstatusfree"])

    def test_leakcheck_handles_hash_in_db_path(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        odd = d / "u#sage.db"
        shutil.copy(self.db, odd)
        self.assertEqual(leakcheck.leaked(odd, SECRETS["project_name"]), [SECRETS["project_name"]])
        self.assertFalse([p for p in d.iterdir() if not p.name.startswith("u#sage.db")])  # no stray empty db at the truncated name

    def test_completed_flat_verdict_is_not_no_change_yet(self):
        db = _corpus(self, reduced=False)
        conn = sqlite3.connect(db)
        init_db(conn)
        fixes.record_fix(conn, SECRETS["project_name"], "resident_output", "n", FIX_DATE)
        conn.close()
        card.write(db, self.out / "f.html")
        text = (self.out / "f.html").read_text(encoding="utf-8")
        self.assertIn("No material change", text)
        self.assertNotIn("No verified change yet", text)

    def test_no_verified_change_is_said_plainly(self):
        db = _corpus(self, reduced=False)
        card.write(db, self.out / "n.html")
        text = (self.out / "n.html").read_text(encoding="utf-8")
        self.assertIn("No verified change yet", text)
        leakcheck.assert_no_leak(self, db, text)

    def test_markup_in_values_is_escaped(self):
        m = card.build(self.db)
        m["causes"][0]["label"] = "<script>x</script>"
        self.assertNotIn("<script>x", card.render_html(m))
        self.assertNotIn("<script>x", card.render_svg(m))

    def test_cli_and_bad_extension(self):
        with mock.patch.object(cli, "DB_PATH", Path(self.db)), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_card(["--out", str(self.out / "x.html")])
            with self.assertRaises(SystemExit):
                cli.cmd_card(["--out", str(self.out / "x.png")])
        self.assertTrue((self.out / "x.html").exists())


class CardConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.db = _corpus(self)

    def _commit_elsewhere(self):
        c = sqlite3.connect(self.db)
        c.execute("UPDATE turns SET output_tokens = output_tokens + 1 WHERE rowid = (SELECT MIN(rowid) FROM turns)")
        c.commit()
        c.close()

    def test_a_commit_during_the_build_makes_it_rebuild(self):
        real, calls = card._build, []

        def build_with_scan(*a):
            calls.append(1)
            if len(calls) == 1:
                self._commit_elsewhere()  # a scan lands between the card's queries
            return real(*a)
        with mock.patch.object(card, "_build", build_with_scan), mock.patch.object(card.time, "sleep"):
            card.build(self.db)
        self.assertEqual(len(calls), 2)

    def test_a_database_that_keeps_changing_is_refused(self):
        real = card._build

        def always_scanning(*a):
            self._commit_elsewhere()
            return real(*a)
        with mock.patch.object(card, "_build", always_scanning), mock.patch.object(card.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "kept changing"):
                card.build(self.db, wait=2)

    def test_period_uses_the_dashboards_calendar(self):
        import api
        # a timestamp whose local date differs from its UTC date at that date's own offset (DST-safe)
        picks = [t for t in ("2020-01-01T00:30:00Z", "2020-01-01T23:30:00Z", "2020-07-01T00:30:00Z",
                             "2020-07-01T23:30:00Z") if api._day_of(t, "local") != t[:10]]
        if not picks:
            self.skipTest("local time equals UTC on the probe dates")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE turns SET timestamp=? WHERE rowid=(SELECT rowid FROM turns ORDER BY timestamp LIMIT 1)", (picks[0],))
        conn.commit()
        conn.close()
        local, utc = card.build(self.db), card.build(self.db, tz="utc")
        self.assertEqual(local["period"][0], api._day_of(picks[0], "local"))
        self.assertEqual(utc["period"][0], picks[0][:10])
        self.assertIn("(local time)", card._lines(local)[1][1])
        self.assertIn("(UTC)", card._lines(utc)[1][1])


if __name__ == "__main__":
    unittest.main()

