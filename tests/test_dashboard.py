"""Tests for api.py - API endpoints, data retrieval, and the static frontend."""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

import pricing


def local_day(iso_utc_ts):
    """Same conversion the server does for local-day bucketing (date(...,
    'localtime')), computed in Python so day assertions are timezone-robust
    instead of assuming the test machine runs near UTC."""
    return datetime.fromisoformat(iso_utc_ts.replace("Z", "+00:00")).astimezone().date().isoformat()
from scanner import get_db, init_db, upsert_sessions, insert_turns
from api import (
    get_dashboard_data, get_session_detail, get_session_breakdown,
    get_sessions_page, get_daily, DashboardHandler,
)

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None

# The frontend is real files under static/. Read them once so the
# content assertions below can check the markup + ES modules. SRC is the concatenation, used
# when a snippet could live in any of them.
_STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (_STATIC / "modules" / "app.js").read_text(encoding="utf-8")
PRICING_JS = (_STATIC / "modules" / "pricing.js").read_text(encoding="utf-8")
FORMAT_JS = (_STATIC / "modules" / "format.js").read_text(encoding="utf-8")
API_JS = (_STATIC / "modules" / "api.js").read_text(encoding="utf-8")
NAV_JS = (_STATIC / "modules" / "nav.js").read_text(encoding="utf-8")
THEME_JS = (_STATIC / "modules" / "theme.js").read_text(encoding="utf-8")
SRC = "\n".join([INDEX_HTML, APP_JS, PRICING_JS, FORMAT_JS, API_JS])


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("tool_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)
        self.assertEqual(session["cache_read"], 500)
        self.assertEqual(session["cache_creation"], 200)
        self.assertEqual(session["full_session_id"], "sess-abc123")
        self.assertEqual(session["tools"], ["<none>"])

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_truncated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(len(session["session_id"]), 8)

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        self.assertTrue(all(r["day"] == local_day("2026-04-08T09:30:00Z") for r in rows))

    def test_tool_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["tool_by_model"]
        self.assertGreater(len(rows), 0)
        self.assertIn("tool", rows[0])
        self.assertIn("cache_read", rows[0])

    def test_session_detail_returns_turns(self):
        data = get_session_detail("sess-abc123", db_path=self.db_path)
        self.assertEqual(data["session"]["session_id"], "sess-abc123")
        self.assertEqual(data["session"]["cache_read"], 500)
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(data["turns"][0]["input"], 500)

    def test_session_detail_missing_session(self):
        data = get_session_detail("missing", db_path=self.db_path)
        self.assertIn("error", data)


class TestSubagentDispatches(unittest.TestCase):
    """get_dashboard_data exposes subagent dispatches from the agents table."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "parent-sess", "project_name": "user/proj",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 0,
        }])
        from scanner import upsert_agents
        upsert_agents(conn, [
            {"agent_id": "ag-1", "agent_type": "Explore",
             "dispatched_in_session": "parent-sess", "completed_at": "2026-04-08T09:30:00Z",
             "status": "completed", "total_tokens": 5000,
             "total_duration_ms": 12000, "tool_use_count": 9},
            {"agent_id": "ag-2", "agent_type": "general-purpose",
             "dispatched_in_session": "parent-sess", "completed_at": "2026-04-08T09:40:00Z",
             "status": "completed", "total_tokens": 8000,
             "total_duration_ms": 20000, "tool_use_count": 15},
            {"agent_id": "ag-3", "agent_type": "Explore",
             "dispatched_in_session": "parent-sess", "completed_at": "2026-04-08T09:50:00Z",
             "status": "completed", "total_tokens": 1000,
             "total_duration_ms": 3000, "tool_use_count": 2},
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_dispatches_present_and_ranked(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("subagent_dispatches", data)
        d = data["subagent_dispatches"]
        self.assertEqual(len(d), 3)
        # Ranked by total_tokens desc.
        self.assertEqual([r["total_tokens"] for r in d], [8000, 5000, 1000])
        top = d[0]
        self.assertEqual(top["agent_type"], "general-purpose")
        self.assertEqual(top["project"], "user/proj")
        self.assertEqual(top["day"], local_day("2026-04-08T09:40:00Z"))
        self.assertEqual(top["tool_use_count"], 15)

    def test_no_dispatches_is_empty_list(self):
        # A DB with no agents rows yields an empty list, not a missing key.
        empty = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        empty.close()
        try:
            conn = get_db(Path(empty.name)); init_db(conn); conn.close()
            data = get_dashboard_data(db_path=Path(empty.name))
            self.assertEqual(data["subagent_dispatches"], [])
        finally:
            os.unlink(empty.name)


class TestEmptyStringModelNormalization(unittest.TestCase):
    """Regression: turns with model='' (empty string) must group as 'unknown'.
    COALESCE(model, 'unknown') alone returns '' because empty string isn't NULL;
    NULLIF(model, '') is needed first."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:05:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-empty", "timestamp": "2026-04-08T09:05:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("unknown", data["all_models"])
        self.assertNotIn("", data["all_models"])

    def test_daily_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["daily_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)

    def test_hourly_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["hourly_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)


class TestMixedNullAndEmptyModel(unittest.TestCase):
    """Regression: a mix of model=NULL and model='' rows must collapse into a
    SINGLE 'unknown' group across all aggregations. Without `GROUP BY
    COALESCE(NULLIF(model, ''), 'unknown')` (matching the SELECT expression),
    SQLite groups by raw value and emits two distinct 'unknown' rows."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mix", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        # Insert one turn with model='' and one with model=NULL on the same day.
        # Use raw INSERT for the NULL row because insert_turns() requires the
        # model key to exist (would error on missing key, not on None).
        insert_turns(conn, [{
            "session_id": "sess-mix", "timestamp": "2026-04-08T09:00:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-mix', '2026-04-08T09:30:00Z', NULL, 100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknowns = [m for m in data["all_models"] if m == "unknown"]
        self.assertEqual(len(unknowns), 1, f"got duplicate 'unknown' rows: {data['all_models']}")

    def test_daily_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknown_rows = [r for r in data["daily_by_model"] if r["model"] == "unknown"]
        # One day, one model bucket
        self.assertEqual(len(unknown_rows), 1, f"got {unknown_rows}")
        self.assertEqual(unknown_rows[0]["turns"], 2)
        self.assertEqual(unknown_rows[0]["input"], 200)

    def test_hourly_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        # Both turns are in UTC hour 9 — must be one row, not two
        hour9 = [r for r in data["hourly_by_model"]
                 if r["hour"] == 9 and r["model"] == "unknown"]
        self.assertEqual(len(hour9), 1, f"got {hour9}")
        self.assertEqual(hour9[0]["turns"], 2)


class TestEffortByModel(unittest.TestCase):
    """`effort_by_model` buckets turns by reasoning effort. Turns that never
    reported one arrive as either '' (parsed post-migration) or NULL (rows
    written before the column existed) and must collapse into ONE 'unknown'."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-eff", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "claude-opus-5",
            "total_input_tokens": 400, "total_output_tokens": 200,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 4,
        }])
        base = {
            "session_id": "sess-eff", "model": "claude-opus-5",
            "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
        }
        insert_turns(conn, [
            dict(base, timestamp="2026-04-08T09:00:00Z", effort="high"),
            dict(base, timestamp="2026-04-08T09:10:00Z", effort="high"),
            dict(base, timestamp="2026-04-08T09:20:00Z"),  # no effort → ''
        ])
        # A pre-migration row: effort is NULL, not ''.
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-eff', '2026-04-08T09:30:00Z', 'claude-opus-5',
                    100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_present_in_payload(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("effort_by_model", data)

    def test_buckets_by_effort(self):
        rows = get_dashboard_data(db_path=self.db_path)["effort_by_model"]
        high = [r for r in rows if r["effort"] == "high"]
        self.assertEqual(len(high), 1, f"got {high}")
        self.assertEqual(high[0]["turns"], 2)
        self.assertEqual(high[0]["input"], 200)
        self.assertEqual(high[0]["model"], "claude-opus-5")

    def test_null_and_empty_collapse_to_single_unknown(self):
        rows = get_dashboard_data(db_path=self.db_path)["effort_by_model"]
        unknown = [r for r in rows if r["effort"] == "unknown"]
        self.assertEqual(len(unknown), 1, f"got {unknown}")
        self.assertEqual(unknown[0]["turns"], 2)


class TestDefaultModelSelectionShowsAll(unittest.TestCase):
    """Regression: local-LLM / non-billable runs (qwen, gemma, glm — or turns
    with no model field) must NOT be hidden by default, even when billable
    Anthropic runs coexist in the same data. The model filter controls
    visibility, not billing, so the default selection is ALL models; cost
    columns still show N/A for non-billable models via isBillable()."""

    def test_default_model_selection_is_all_models(self):
        # The selection logic is JS; assert the source defaults to all models so
        # a future refactor doesn't silently re-introduce the billable-only
        # default that hid local-LLM runs.
        self.assertIn("if (!param) return new Set(allModels)", APP_JS)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs + the Codex home to a tempdir so
        # /api/rescan doesn't unlink the user's real ~/.claude/usage.db or
        # scan their real transcript directories (~/.claude, ~/.codex)
        # during tests.
        import api as _d
        import scanner as _s
        import sources.codex as _c
        import sources as _m
        import config as _cfg
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        tmp_codex = tmp / "codex"
        tmp_codex.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
            (_c, "CODEX_PATH"):             (_c.CODEX_PATH,             tmp_codex),
            (_cfg, "DESKTOP_BLOB_DIR"):     (_cfg.DESKTOP_BLOB_DIR,     tmp / "no-desktop-cache"),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        DashboardHandler.token = "test-token"
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"whytokens", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?_=cachebust"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_pricing_returns_table(self):
        # The single source of truth for the price table, served to the frontend.
        import pricing
        url = f"http://127.0.0.1:{self.port}/api/pricing"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertEqual(data, pricing.PRICING)
            self.assertIn("claude-opus-4-8", data)

    def test_static_css_served_with_content_type(self):
        url = f"http://127.0.0.1:{self.port}/static/styles.css"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers["Content-Type"])

    def test_static_module_served_as_javascript(self):
        url = f"http://127.0.0.1:{self.port}/static/modules/app.js"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers["Content-Type"])

    def test_static_path_traversal_blocked(self):
        # "/static/../api.py" must not escape static/ and serve source files.
        url = f"http://127.0.0.1:{self.port}/static/../api.py"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404 for path traversal")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_api_sessions_returns_paginated_json(self):
        url = f"http://127.0.0.1:{self.port}/api/sessions?limit=5&sort=tokens"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Either a paginated payload or an error if the temp DB is empty.
            self.assertTrue(
                ("sessions" in data and "total" in data and "limit" in data)
                or "error" in data
            )

    def test_api_daily_returns_aggregated_json(self):
        url = f"http://127.0.0.1:{self.port}/api/daily?model=claude-opus-4-8"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertTrue(
                ("daily" in data and "totals" in data) or "error" in data
            )

    def test_api_session_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/session?session_id=missing"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("error", data)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST",
            headers={"X-Whytokens-Token": "test-token"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_rescan_preserves_experiments_satellite(self):
        # A destructive rescan unlinks + rebuilds the DB from transcripts. Satellite
        # tables the scanner doesn't own (e.g. an eval harness's `experiments` table)
        # must survive — otherwise every rescan silently drops the eval data.
        import api as _api
        db = _api.DB_PATH
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS experiments "
                    "(exp_id TEXT PRIMARY KEY, arm TEXT, cost_usd REAL)")
        con.execute("INSERT OR REPLACE INTO experiments VALUES ('x1', 'baseline', 1.23)")
        con.commit()
        con.close()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/rescan", method="POST",
            headers={"X-Whytokens-Token": "test-token"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT exp_id, arm, cost_usd FROM experiments").fetchall()
        finally:
            con.close()
        self.assertIn(("x1", "baseline", 1.23), rows)

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class TestRescanMultiSource(unittest.TestCase):
    """Regression: POST /api/rescan must rebuild via sources.scan_all so Codex
    rows survive a dashboard-triggered rescan. Calling the
    Claude-only scanner.scan() would delete the DB (Codex rows included)
    and re-ingest only Claude, silently destroying all Codex history."""

    @classmethod
    def setUpClass(cls):
        import api as _d
        import scanner as _s
        import sources.codex as _c
        import sources as _m
        import config as _cfg

        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        fixtures = Path(__file__).resolve().parent / "fixtures"

        # Claude fixture: a project dir holding the golden transcript.
        tmp_projects = tmp / "projects"
        (tmp_projects / "proj").mkdir(parents=True)
        shutil.copy(fixtures / "claude" / "golden.jsonl",
                    tmp_projects / "proj" / "golden.jsonl")

        # Codex fixture: a CODEX_HOME with one rollout under sessions/.
        tmp_codex = tmp / "codex"
        sess = tmp_codex / "sessions" / "2026" / "01" / "01"
        sess.mkdir(parents=True)
        shutil.copy(fixtures / "codex" / "golden-codex.jsonl",
                    sess / "rollout-a.jsonl")

        cls.db_path = tmp / "usage.db"
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                cls.db_path),
            (_s, "DB_PATH"):                (_s.DB_PATH,                cls.db_path),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
            (_c, "CODEX_PATH"):             (_c.CODEX_PATH,             tmp_codex),
            (_cfg, "DESKTOP_BLOB_DIR"):     (_cfg.DESKTOP_BLOB_DIR,     tmp / "no-desktop-cache"),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        DashboardHandler.token = "test-token"
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def _rescan(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST",
            headers={"X-Whytokens-Token": "test-token"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read())

    def _agent_turn_counts(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT agent, COUNT(*) FROM turns GROUP BY agent"
            ).fetchall()
        finally:
            conn.close()
        return dict(rows)

    def test_rescan_ingests_both_sources_and_codex_survives_rerun(self):
        data = self._rescan()

        # Flat totals keep the original frontend contract (.new/.updated ...)
        for key in ("new", "updated", "skipped", "turns", "sessions"):
            self.assertIn(key, data, f"flat key {key!r} missing from response")
        # ... and per-source detail is broken out under "sources".
        self.assertIn("claude", data["sources"])
        self.assertIn("codex", data["sources"])
        self.assertEqual(data["sources"]["codex"]["turns"], 3)
        self.assertGreater(data["sources"]["claude"]["turns"], 0)
        self.assertEqual(
            data["turns"],
            data["sources"]["claude"]["turns"] + data["sources"]["codex"]["turns"],
        )

        counts = self._agent_turn_counts()
        self.assertEqual(counts.get("codex"), 3)
        self.assertGreater(counts.get("claude", 0), 0)

        # The destructive path again: a second rescan drops the DB and rebuilds.
        # Codex rows must survive (this is exactly what scanner.scan() lost).
        self._rescan()
        self.assertEqual(self._agent_turn_counts().get("codex"), 3)


class TestStaticAssets(unittest.TestCase):
    """The frontend is real files under static/ (index.html + ES modules).
    These assertions guard the dashboard behaviors,
    pointed at whichever file the snippet now lives in (SRC = all of them)."""

    def test_index_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", INDEX_HTML)
        self.assertIn("</html>", INDEX_HTML)

    def test_has_esc_function(self):
        """Verify XSS protection is present."""
        self.assertIn("function esc(", FORMAT_JS)

    def test_chart_js_vendored_locally_no_cdn(self):
        """Chart.js is served from the local vendor copy, not a CDN (offline)."""
        self.assertIn("/static/vendor/chart.umd.min.js", INDEX_HTML)
        self.assertNotIn("jsdelivr", INDEX_HTML)
        self.assertNotIn("cdn.", INDEX_HTML)

    def test_frontend_single_sources_pricing(self):
        """The price table must be fetched from /api/pricing, never hardcoded in
        the JS (single source of truth lives in pricing.py)."""
        self.assertIn("/api/pricing", API_JS)
        self.assertIn("fetchPricing", APP_JS)
        # No hardcoded price table in the JS — DEFAULT_PRICING starts empty and is
        # filled from the server. Guard against a model->rate literal creeping back.
        self.assertNotIn("'claude-opus-4-8':   {", PRICING_JS)
        self.assertNotIn("output: 25.00", PRICING_JS)

    def test_modules_are_es_modules(self):
        """index.html loads app.js as a module; app.js imports the leaf modules."""
        self.assertIn('type="module"', INDEX_HTML)
        self.assertIn("import {", APP_JS)
        self.assertIn("export function", FORMAT_JS)

    def test_inline_handlers_exposed_on_window(self):
        """ES-module scope isn't global, so inline on*= handlers must be re-exposed."""
        self.assertIn("Object.assign(window", APP_JS)
        for handler in ("setRange", "openSessionDetail", "onPricingChange",
                        "closeToolPanel", "moreSessionRows", "exportModelCSV"):
            self.assertIn(handler, APP_JS)

    def test_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models.
        "opus-" (not bare "opus") so local builds ending in "opus" stay unbilled."""
        self.assertIn("m.includes('fable')", PRICING_JS)
        self.assertIn("m.includes('opus-')", PRICING_JS)
        self.assertIn("m.includes('sonnet')", PRICING_JS)
        self.assertIn("m.includes('haiku')", PRICING_JS)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", PRICING_JS)

    def test_sessions_priced_per_model_not_by_label(self):
        """Session/project/branch cost must price each model's tokens at its own
        rate (sessionCost over s.by_model), not the single label x summed tokens —
        else any session with a differently-modeled subagent is mispriced."""
        self.assertIn("export function sessionCost", PRICING_JS)
        self.assertIn("by_model", PRICING_JS)
        self.assertIn("sessionCost(s, s.last_date)", APP_JS)
        # The buggy single-label session pricing must not reappear in app.js.
        self.assertNotIn("calcCost(s.model, s.input", APP_JS)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', INDEX_HTML)
        self.assertIn('data-tz="local"', INDEX_HTML)
        self.assertIn('data-tz="utc"', INDEX_HTML)

    def test_hourly_peak_hour_constants(self):
        """Peak-hour set covers UTC 12–17 (Mon–Fri 05:00–11:00 PT)."""
        self.assertIn('PEAK_HOURS_UTC', APP_JS)
        self.assertIn('[12, 13, 14, 15, 16, 17]', APP_JS)

    def test_recent_sessions_shows_cache_tokens(self):
        """Recent sessions table exposes session-level cache token usage."""
        self.assertIn("setSessionSort('cache_read')", INDEX_HTML)
        self.assertIn("setSessionSort('cache_creation')", INDEX_HTML)
        self.assertIn("${fmt(s.cache_read)}", APP_JS)
        self.assertIn("${fmt(s.cache_creation)}", APP_JS)

    def test_has_drilldown_and_metric_tables(self):
        self.assertIn('id="session-detail-view"', INDEX_HTML)
        self.assertIn('id="chart-session-timeline"', INDEX_HTML)
        self.assertIn('id="session-tool-breakdown-body"', INDEX_HTML)
        self.assertIn('id="cost-breakdown-body"', INDEX_HTML)
        self.assertIn('id="tool-cost-body"', INDEX_HTML)
        self.assertIn('id="session-signals-body"', INDEX_HTML)
        self.assertIn("function openSessionDetail(", APP_JS)
        self.assertIn("function closeSessionDetail(", APP_JS)
        # Trace is a screen now: the session id rides in the URL while it is active.
        self.assertIn("params.set('session'", APP_JS)
        self.assertIn('id="panel-trace"', INDEX_HTML)
        self.assertIn("function renderSessionToolBreakdown(", APP_JS)

    def test_has_session_filters(self):
        self.assertIn('id="session-project-filter"', INDEX_HTML)
        self.assertIn('id="session-branch-filter"', INDEX_HTML)
        self.assertIn('id="session-tool-filter"', INDEX_HTML)
        self.assertIn('id="session-min-cost-filter"', INDEX_HTML)
        self.assertIn('id="session-cache-filter"', INDEX_HTML)
        self.assertIn("function applySessionFilters(", APP_JS)

    def test_has_editable_pricing(self):
        self.assertIn('id="pricing-body"', INDEX_HTML)
        self.assertIn("PRICING_STORAGE_KEY", PRICING_JS)
        self.assertIn("function renderPricingEditor(", APP_JS)
        self.assertIn("function onPricingChange(", APP_JS)
        self.assertIn("function resetPricing(", APP_JS)

    def test_hourly_filter_uses_range_bounds(self):
        """Hourly filter should use the same date bounds as the rest of the UI."""
        self.assertNotIn("cutoff", SRC)
        self.assertIn("(!start || r.day >= start) && (!end || r.day <= end)", APP_JS)

    def test_today_range_button_present(self):
        """The 'Today' range button is wired into RANGE_LABELS, RANGE_TICKS,
        getRangeBounds, and the filter-bar HTML."""
        self.assertIn("data-range=\"today\"", INDEX_HTML)
        self.assertIn("'today': 'Today'", APP_JS)
        self.assertIn("'today': 1", APP_JS)
        # Bounds case: today returns start === end === today's ISO date
        self.assertIn("range === 'today'", APP_JS)


class TestTabbedLayout(unittest.TestCase):
    """Tabbed shell, lazy per-tab render, collapsible
    secondary tables, and expandable project rows. These guard the
    structure the JS reaches for by id/class/function name."""

    def test_tab_bar_has_all_panels(self):
        """A rail button per screen, and a matching panel for each."""
        self.assertIn('id="tab-bar"', INDEX_HTML)
        for tab in ("overview", "sessions", "trace", "waste", "sources", "settings"):
            self.assertIn(f'data-tab="{tab}"', INDEX_HTML)
            self.assertIn(f'data-panel="{tab}"', INDEX_HTML)
        # Folded screens live inside their new homes, not as screens of their own.
        for tab in ("projects", "tools", "pricing"):
            self.assertNotIn(f'data-tab="{tab}"', INDEX_HTML)
            self.assertIn(f'id="{tab}"', INDEX_HTML)

    def test_only_overview_tab_open_by_default(self):
        """Overview is the default tab; the other panels start hidden."""
        # The overview rail item is the current page.
        overview_btn = INDEX_HTML[INDEX_HTML.index('data-tab="overview"') - 80:
                                  INDEX_HTML.index('data-tab="overview"') + 80]
        self.assertIn('aria-current="page"', overview_btn)
        # Non-overview panels start hidden (attribute order-independent).
        for panel in ("sessions", "trace", "waste", "sources", "settings"):
            seg = INDEX_HTML[INDEX_HTML.index(f'data-panel="{panel}"'):
                             INDEX_HTML.index(f'data-panel="{panel}"') + 120]
            self.assertIn(" hidden", seg)

    def test_tabs_have_aria_wiring(self):
        """Rail items are linked to their panels and named; aria-current marks the
        current screen (set in nav.js applyTabVisibility). The rail is a nav, not a tablist."""
        self.assertIn('aria-controls="panel-overview"', INDEX_HTML)
        self.assertIn('aria-label="Overview"', INDEX_HTML)
        self.assertIn("setAttribute('aria-current', 'page')", NAV_JS)

    def test_aggregate_render_split(self):
        """applyFilter is split into a pure aggregate() and a per-tab renderTab()."""
        self.assertIn("function aggregate(", APP_JS)
        self.assertIn("function renderTab(", APP_JS)
        self.assertIn("function setTab(", APP_JS)
        # applyFilter now delegates rather than fanning out every renderer itself.
        self.assertIn("aggregate();", APP_JS)
        self.assertIn("renderTab(activeTab);", APP_JS)

    def test_active_tab_persisted_in_url(self):
        """The active tab round-trips through the URL alongside range/models."""
        self.assertIn("function readURLTab(", APP_JS)
        self.assertIn("params.set('tab', activeTab)", APP_JS)
        self.assertIn("VALID_TABS", APP_JS)

    def test_secondary_tables_are_collapsible(self):
        """Cost Breakdown, Session Signals, and Project & Branch are <details>."""
        self.assertIn('<details class="table-card collapsible">', INDEX_HTML)
        # The collapsibles still carry their original tbody ids.
        for body in ("cost-breakdown-body", "session-signals-body",
                     "project-branch-cost-body"):
            self.assertIn(f'id="{body}"', INDEX_HTML)

    def test_project_rows_expandable_to_branches(self):
        """Project rows expand to per-branch sub-rows via a delegated handler."""
        self.assertIn("function wireProjectExpand(", APP_JS)
        self.assertIn("row-toggle", APP_JS)
        self.assertIn("branch-subrow", APP_JS)

    def test_core_static_has_no_experiments_and_ext_tabs_drive_valid_tabs(self):
        """Experiments lives in ext/experiments; core only knows the generic
        extension-tab seam (GET /api/extensions -> VALID_TABS + dynamic import)."""
        for name, src in (("index.html", INDEX_HTML), ("app.js", APP_JS), ("api.js", API_JS)):
            self.assertNotIn("xperiment", src, name)
        self.assertIn("VALID_TABS.push(t.name)", APP_JS)
        self.assertLess(APP_JS.index("await loadExtensionTabs()"), APP_JS.index("  wireTabs();"))
        self.assertIn("import(extTabs.get(name).module_url)", APP_JS)

    def test_charts_render_on_tab_activate(self):
        """Charts are built inside renderTab's overview case (not eagerly in
        aggregate), so a hidden tab's canvas never measures 0×0."""
        render_tab_idx = APP_JS.index("function renderTab(")
        agg_idx = APP_JS.index("function aggregate(")
        # The chart builders are called from renderTab, after aggregate ends.
        self.assertIn("renderDailyChart(lastDaily)", APP_JS)
        self.assertGreater(APP_JS.index("renderDailyChart(lastDaily)"), render_tab_idx)
        # aggregate() must not paint charts itself.
        agg_body = APP_JS[agg_idx:render_tab_idx]
        self.assertNotIn("renderDailyChart(", agg_body)
        self.assertNotIn("renderHourlyChart(", agg_body)


def _seed_multi(db_path):
    """Three sessions across two projects / two models / three days, with
    matching turns, for the parameterized-endpoint tests."""
    conn = get_db(db_path)
    init_db(conn)
    sessions = [
        {"session_id": "sess-1aaaaaa", "project_name": "alpha",
         "first_timestamp": "2026-05-01T09:00:00Z", "last_timestamp": "2026-05-01T10:00:00Z",
         "git_branch": "main", "model": "claude-opus-4-8",
         "total_input_tokens": 1000, "total_output_tokens": 200,
         "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2},
        {"session_id": "sess-2bbbbbb", "project_name": "beta",
         "first_timestamp": "2026-05-02T09:00:00Z", "last_timestamp": "2026-05-02T10:00:00Z",
         "git_branch": "dev", "model": "claude-sonnet-4-6",
         "total_input_tokens": 500, "total_output_tokens": 100,
         "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1},
        {"session_id": "sess-3cccccc", "project_name": "alpha",
         "first_timestamp": "2026-05-03T09:00:00Z", "last_timestamp": "2026-05-03T10:00:00Z",
         "git_branch": "main", "model": "claude-opus-4-8",
         "total_input_tokens": 2000, "total_output_tokens": 400,
         "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2},
    ]
    upsert_sessions(conn, sessions)
    turns = [
        {"session_id": "sess-1aaaaaa", "timestamp": "2026-05-01T09:30:00Z",
         "model": "claude-opus-4-8", "input_tokens": 500, "output_tokens": 100,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": "Read", "cwd": "/tmp"},
        {"session_id": "sess-1aaaaaa", "timestamp": "2026-05-01T09:45:00Z",
         "model": "claude-opus-4-8", "input_tokens": 500, "output_tokens": 100,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": "Grep", "cwd": "/tmp"},
        {"session_id": "sess-2bbbbbb", "timestamp": "2026-05-02T09:30:00Z",
         "model": "claude-sonnet-4-6", "input_tokens": 500, "output_tokens": 100,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp"},
        {"session_id": "sess-3cccccc", "timestamp": "2026-05-03T09:30:00Z",
         "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 200,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": "Read", "cwd": "/tmp"},
        {"session_id": "sess-3cccccc", "timestamp": "2026-05-03T09:45:00Z",
         "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 200,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": "Edit", "cwd": "/tmp"},
    ]
    insert_turns(conn, turns)
    conn.commit()
    conn.close()


class TestSessionModelBreakdown(unittest.TestCase):
    """Sessions expose a per-model token split so the client prices each model's
    tokens at its own rate. Regression: a fable-5 session that spawned an opus Task
    was priced as one label x summed tokens (~44% off)."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "mixed-001", "project_name": "p",
            "first_timestamp": "2026-06-11T00:00:00Z",
            "last_timestamp": "2026-06-11T00:10:00Z",
            "git_branch": "main", "model": "claude-fable-5",
            "total_input_tokens": 300, "total_output_tokens": 150,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 3,
        }])
        # Two fable MAIN turns + one opus SIDECHAIN (subagent) turn.
        insert_turns(conn, [
            {"session_id": "mixed-001", "timestamp": "2026-06-11T00:01:00Z",
             "model": "claude-fable-5", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "tool_name": None, "cwd": "/p", "message_id": "a", "is_sidechain": 0},
            {"session_id": "mixed-001", "timestamp": "2026-06-11T00:02:00Z",
             "model": "claude-fable-5", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "tool_name": None, "cwd": "/p", "message_id": "b", "is_sidechain": 0},
            {"session_id": "mixed-001", "timestamp": "2026-06-11T00:03:00Z",
             "model": "claude-opus-4-8", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "tool_name": None, "cwd": "/p", "message_id": "c", "is_sidechain": 1},
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _mixed(self, sessions):
        return next(s for s in sessions if s["full_session_id"] == "mixed-001")

    def test_dashboard_includes_by_model_split(self):
        s = self._mixed(get_dashboard_data(self.db_path)["sessions_all"])
        by = {m["model"]: m for m in s["by_model"]}
        self.assertEqual(set(by), {"claude-fable-5", "claude-opus-4-8"})
        self.assertEqual(by["claude-fable-5"]["input"], 200)
        self.assertEqual(by["claude-fable-5"]["output"], 100)
        self.assertEqual(by["claude-opus-4-8"]["input"], 100)

    def test_sessions_page_includes_by_model_split(self):
        s = self._mixed(get_sessions_page(db_path=self.db_path)["sessions"])
        self.assertEqual(len(s["by_model"]), 2)

    def test_per_model_cost_is_correct_not_single_label(self):
        s = self._mixed(get_dashboard_data(self.db_path)["sessions_all"])
        per_model = sum(
            pricing.calc_cost(m["model"], m["input"], m["output"],
                              m["cache_read"], m["cache_creation"])
            for m in s["by_model"])
        single_label = pricing.calc_cost(
            s["model"], s["input"], s["output"], s["cache_read"], s["cache_creation"])
        expected = (pricing.calc_cost("claude-fable-5", 200, 100, 0, 0)
                    + pricing.calc_cost("claude-opus-4-8", 100, 50, 0, 0))
        self.assertAlmostEqual(per_model, expected)
        # The single-label shortcut overprices the opus turn at the fable rate.
        self.assertNotAlmostEqual(per_model, single_label)


class TestGetSessionsPage(unittest.TestCase):
    """get_sessions_page: SQL-side filtering, pagination, allowlisted sort."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        _seed_multi(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _ids(self, result):
        return [s["full_session_id"] for s in result["sessions"]]

    def test_no_filter_returns_all_recent_first(self):
        r = get_sessions_page(db_path=self.db_path)
        self.assertEqual(r["total"], 3)
        self.assertEqual(self._ids(r), ["sess-3cccccc", "sess-2bbbbbb", "sess-1aaaaaa"])

    def test_filter_by_project(self):
        r = get_sessions_page(project="alpha", db_path=self.db_path)
        self.assertEqual(r["total"], 2)
        self.assertEqual(set(self._ids(r)), {"sess-1aaaaaa", "sess-3cccccc"})

    def test_filter_by_model(self):
        r = get_sessions_page(model="claude-sonnet-4-6", db_path=self.db_path)
        self.assertEqual(r["total"], 1)
        self.assertEqual(self._ids(r), ["sess-2bbbbbb"])

    def test_filter_by_date_range(self):
        r = get_sessions_page(start="2026-05-02", db_path=self.db_path)
        self.assertEqual(r["total"], 2)
        self.assertEqual(set(self._ids(r)), {"sess-2bbbbbb", "sess-3cccccc"})

    def test_pagination_total_is_prefilter_count(self):
        r = get_sessions_page(limit=1, offset=0, db_path=self.db_path)
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(r["sessions"]), 1)
        self.assertEqual(self._ids(r), ["sess-3cccccc"])

    def test_pagination_offset(self):
        r = get_sessions_page(limit=1, offset=1, db_path=self.db_path)
        self.assertEqual(self._ids(r), ["sess-2bbbbbb"])

    def test_sort_by_tokens(self):
        r = get_sessions_page(sort="tokens", db_path=self.db_path)
        self.assertEqual(self._ids(r)[0], "sess-3cccccc")  # 2400 tokens

    def test_invalid_sort_falls_back_to_recent(self):
        r = get_sessions_page(sort="DROP TABLE sessions", db_path=self.db_path)
        self.assertEqual(r["sort"], "recent")
        self.assertEqual(self._ids(r)[0], "sess-3cccccc")

    def test_limit_clamped_high(self):
        r = get_sessions_page(limit=99999, db_path=self.db_path)
        self.assertEqual(r["limit"], 500)

    def test_limit_clamped_low(self):
        r = get_sessions_page(limit=0, db_path=self.db_path)
        self.assertEqual(r["limit"], 1)

    def test_junk_limit_offset_defaults(self):
        r = get_sessions_page(limit="abc", offset="xyz", db_path=self.db_path)
        self.assertEqual(r["limit"], 50)
        self.assertEqual(r["offset"], 0)

    def test_filter_model_unknown_matches_null_and_empty(self):
        # A session stored with NULL/'' model is serialized as "unknown"; the
        # model filter must round-trip that — ?model=unknown returns it.
        conn = get_db(self.db_path)
        upsert_sessions(conn, [{
            "session_id": "sess-4nullmod", "project_name": "gamma",
            "first_timestamp": "2026-05-04T09:00:00Z",
            "last_timestamp": "2026-05-04T10:00:00Z",
            "git_branch": "main", "model": None,
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }])
        conn.commit()
        conn.close()
        r = get_sessions_page(model="unknown", db_path=self.db_path)
        self.assertEqual(self._ids(r), ["sess-4nullmod"])
        self.assertEqual(r["total"], 1)

    def test_missing_db_returns_error(self):
        r = get_sessions_page(db_path=Path("/nonexistent/x.db"))
        self.assertIn("error", r)


class TestGetDaily(unittest.TestCase):
    """get_daily: SQL-side aggregation + server-side cost."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        _seed_multi(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_no_filter_aggregates_all_days(self):
        r = get_daily(db_path=self.db_path)
        days = sorted({row["day"] for row in r["daily"]})
        self.assertEqual(days, ["2026-05-01", "2026-05-02", "2026-05-03"])
        self.assertEqual(r["totals"]["input"], 3500)
        self.assertEqual(r["totals"]["output"], 700)
        self.assertEqual(r["totals"]["turns"], 5)

    def test_cost_computed_server_side(self):
        r = get_daily(model="claude-opus-4-8", db_path=self.db_path)
        # All rows are opus; cost must be positive and match the price table.
        self.assertGreater(r["totals"]["cost"], 0)
        import pricing
        # 05-03 opus row: 2000 in, 400 out
        row = next(x for x in r["daily"] if x["day"] == "2026-05-03")
        expected = pricing.calc_cost("claude-opus-4-8", 2000, 400, 0, 0)
        self.assertAlmostEqual(row["cost"], round(expected, 6), places=6)

    def test_filter_by_model(self):
        r = get_daily(model="claude-opus-4-8", db_path=self.db_path)
        self.assertTrue(all(row["model"] == "claude-opus-4-8" for row in r["daily"]))
        self.assertEqual(r["totals"]["input"], 3000)

    def test_filter_by_project(self):
        r = get_daily(project="beta", db_path=self.db_path)
        self.assertEqual(len(r["daily"]), 1)
        self.assertEqual(r["daily"][0]["day"], "2026-05-02")

    def test_filter_by_date_range(self):
        r = get_daily(start="2026-05-02", db_path=self.db_path)
        days = sorted({row["day"] for row in r["daily"]})
        self.assertEqual(days, ["2026-05-02", "2026-05-03"])

    def test_missing_db_returns_error(self):
        r = get_daily(db_path=Path("/nonexistent/x.db"))
        self.assertIn("error", r)


class TestGetDailyTimezone(unittest.TestCase):
    """/api/daily defaults to UTC day bucketing (documented contract); tz=local
    switches to the server machine's local calendar day, DST-aware, matching
    api.get_dashboard_data's own local-day bucketing."""

    # Same UTC calendar day (2026-04-08), one turn near each end of it, so a
    # non-UTC local timezone is likely to push at least one of them onto an
    # adjacent local day — this is timezone-robust because the expected day is
    # computed via Python's own local conversion, not hardcoded.
    LATE_TS = "2026-04-08T23:30:00Z"
    EARLY_TS = "2026-04-08T11:30:00Z"

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-tz", "project_name": "user/proj",
            "first_timestamp": self.EARLY_TS, "last_timestamp": self.LATE_TS,
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 0,
        }])
        insert_turns(conn, [
            {"session_id": "sess-tz", "timestamp": self.LATE_TS,
             "model": "claude-sonnet-4-6", "input_tokens": 10, "output_tokens": 5,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": None,
             "message_id": "m-late", "cwd": "/tmp"},
            {"session_id": "sess-tz", "timestamp": self.EARLY_TS,
             "model": "claude-sonnet-4-6", "input_tokens": 20, "output_tokens": 8,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": None,
             "message_id": "m-early", "cwd": "/tmp"},
        ])
        conn.commit()
        conn.close()

    def test_boundary_turns_land_on_python_local_day(self):
        r = get_daily(tz="local", db_path=self.db_path)
        days = {row["day"] for row in r["daily"]}
        self.assertEqual(days, {local_day(self.LATE_TS), local_day(self.EARLY_TS)})
        self.assertEqual(r["filters"]["tz"], "local")

    def test_default_stays_utc(self):
        r = get_daily(db_path=self.db_path)
        # Default bucketing is the documented UTC contract: both turns are on
        # the same UTC calendar day regardless of the test machine's timezone.
        days = {row["day"] for row in r["daily"]}
        self.assertEqual(days, {"2026-04-08"})
        self.assertEqual(r["filters"]["tz"], "utc")


class TestSessionLastDateTimezone(unittest.TestCase):
    """`last_date` must be the LOCAL calendar day.

    The dashboard range-filters sessions on `last_date` against local bounds
    (app.js localISODate) — the same bounds the turn/day buckets already use. While
    this was a UTC slice, the Sessions count card and the token cards classified the
    same session into different days near local midnight.

    The fixture timestamp is picked by the sign of the local UTC offset, because no
    fixed hour crosses local midnight in both eastern and western zones. On a
    machine at UTC nothing here can discriminate, so those assertions skip.
    """

    SHIFTED_TS = ("2026-04-08T23:30:00Z"
                  if datetime.now().astimezone().utcoffset().total_seconds() > 0
                  else "2026-04-08T00:30:00Z")

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-lastdate", "project_name": "user/proj",
            "first_timestamp": self.SHIFTED_TS, "last_timestamp": self.SHIFTED_TS,
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-lastdate", "timestamp": self.SHIFTED_TS,
            "model": "claude-sonnet-4-6", "input_tokens": 10, "output_tokens": 5,
            "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": None,
            "message_id": "m-lastdate", "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _row(self):
        data = get_dashboard_data(db_path=self.db_path)
        return next(s for s in data["sessions_all"]
                    if s["full_session_id"] == "sess-lastdate")

    def test_last_date_matches_the_turn_day_bucket(self):
        expected = local_day(self.SHIFTED_TS)
        self.assertEqual(self._row()["last_date"], expected)
        # The whole point: the session lands on the same day its own turn does.
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual({r["day"] for r in data["daily_by_model"]}, {expected})

    def test_last_date_is_not_the_utc_slice(self):
        if local_day(self.SHIFTED_TS) == self.SHIFTED_TS[:10]:
            self.skipTest("machine is at UTC — local and UTC days coincide")
        self.assertNotEqual(self._row()["last_date"], self.SHIFTED_TS[:10])


if __name__ == "__main__":
    unittest.main()


class TestSessionBreakdown(unittest.TestCase):
    """GET /api/session-breakdown: who spent a session — main thread vs each
    dispatched subagent type. The load-bearing property is that the buckets are
    DISJOINT and COMPLETE: they must re-sum to the session's own totals, or the
    dashboard's sub-rows quietly stop adding up to the row above them."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        from scanner import upsert_agents
        upsert_agents(conn, [
            {"agent_id": "ag-1", "agent_type": "task-implementer",
             "dispatched_in_session": "sess-1", "completed_at": None,
             "status": "completed", "total_tokens": None,
             "total_duration_ms": None, "tool_use_count": None},
            # Deliberately untyped: an agent the sweep never reached must fall back
            # to the main-thread bucket rather than vanish from the totals.
            {"agent_id": "ag-untyped", "agent_type": None,
             "dispatched_in_session": "sess-1", "completed_at": None,
             "status": None, "total_tokens": None,
             "total_duration_ms": None, "tool_use_count": None},
        ])

        def turn(mid, agent_id=None, model="claude-opus-4-8", inp=100, out=50):
            return {
                "session_id": "sess-1", "timestamp": "2026-04-08T10:00:00Z",
                "model": model, "input_tokens": inp, "output_tokens": out,
                "cache_read_tokens": 1000, "cache_creation_tokens": 200,
                "cache_creation_5m_tokens": 50, "cache_creation_1h_tokens": 150,
                "tool_name": None, "cwd": "/tmp", "message_id": mid,
                "duration_ms": 0, "stop_reason": "", "service_tier": "",
                "inference_geo": "", "is_sidechain": bool(agent_id),
                "is_compact_summary": False, "tool_calls": None,
                "agent": "claude", "reasoning_tokens": 0, "agent_id": agent_id,
                "effort": None,
            }
        insert_turns(conn, [
            turn("m1"), turn("m2"),
            turn("m3", agent_id="ag-1"),
            turn("m4", agent_id="ag-1", model="claude-sonnet-4-6"),
            turn("m5", agent_id="ag-untyped"),
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _rows(self):
        return get_session_breakdown("sess-1", db_path=self.db_path)["rows"]

    def test_buckets_by_agent_type_and_model(self):
        rows = self._rows()
        labels = {(r["label"], r["model"]): r for r in rows}
        self.assertEqual(labels[("task-implementer", "claude-opus-4-8")]["turns"], 1)
        self.assertEqual(labels[("task-implementer", "claude-sonnet-4-6")]["turns"], 1)
        self.assertEqual(labels[("task-implementer", "claude-opus-4-8")]["kind"], "agent")
        # 2 plain turns + the untyped agent's turn.
        self.assertEqual(labels[("main thread", "claude-opus-4-8")]["turns"], 3)
        self.assertEqual(labels[("main thread", "claude-opus-4-8")]["kind"], "main")

    def test_buckets_resum_to_the_session_totals(self):
        rows = self._rows()
        conn = get_db(self.db_path)
        totals = conn.execute("""
            SELECT COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
                   SUM(cache_read_tokens) cr, SUM(cache_creation_tokens) cc,
                   SUM(cache_creation_1h_tokens) c1
            FROM turns WHERE session_id = 'sess-1'
        """).fetchone()
        conn.close()
        self.assertEqual(sum(r["turns"] for r in rows), totals["n"])
        self.assertEqual(sum(r["input"] for r in rows), totals["i"])
        self.assertEqual(sum(r["output"] for r in rows), totals["o"])
        self.assertEqual(sum(r["cache_read"] for r in rows), totals["cr"])
        self.assertEqual(sum(r["cache_creation"] for r in rows), totals["cc"])
        self.assertEqual(sum(r["cache_creation_1h"] for r in rows), totals["c1"])

    def test_missing_session_id_is_an_error_not_a_crash(self):
        self.assertIn("error", get_session_breakdown("", db_path=self.db_path))

    def test_unknown_session_is_empty(self):
        self.assertEqual(
            get_session_breakdown("nope", db_path=self.db_path)["rows"], [])
