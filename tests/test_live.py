"""Tests for GET /api/live (api.get_live_session) - the transcript-direct read
that hooks and the session-cost skill use for the session currently in progress.

The number this endpoint produces lands in a cost warning the user acts on, so
the checks below are written to FAIL on the ways it could be plausibly wrong:
a parse set that misses the subagent files, cwd resolution picking the wrong
transcript, or the cost stopping at zero.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

import api
import pricing
import scanner

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude" / "golden.jsonl"
CWD = "C:\\work\\acme"
SLUG = "C--work-acme"          # what Claude Code mangles CWD into
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class LiveSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "projects" / SLUG
        (self.proj / SESSION / "subagents").mkdir(parents=True)
        self.main = self.proj / (SESSION + ".jsonl")
        shutil.copy(FIXTURE, self.main)
        self._saved = scanner.DEFAULT_PROJECTS_DIRS
        scanner.DEFAULT_PROJECTS_DIRS = [self.tmp / "projects"]

    def tearDown(self):
        scanner.DEFAULT_PROJECTS_DIRS = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sub(self, name="agent-1.jsonl"):
        """Copy the fixture in as a subagent transcript, with fresh message ids
        (dedup is by message.id, so a verbatim copy would collapse into one) and
        LATER timestamps, so that a context reading which wrongly counted
        sidechain turns would visibly pick one of these up."""
        dst = self.proj / SESSION / "subagents" / name
        out = []
        for line in self.main.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["isSidechain"] = True
            if rec.get("timestamp"):
                rec["timestamp"] = rec["timestamp"].replace("2026-05-01", "2026-05-02")
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("id"):
                msg["id"] = msg["id"] + "-sub"
            # Distinct token counts too: a verbatim copy would make a wrong
            # context reading (one that counted sidechains) look identical.
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(usage, dict):
                for k, v in usage.items():
                    # Only non-zero fields: bumping a zero would turn a skipped
                    # advisor iteration into a real extra turn.
                    if isinstance(v, int) and v:
                        usage[k] = v + 7
            out.append(json.dumps(rec))
        dst.write_text("\n".join(out) + "\n", encoding="utf-8")
        return dst

    # -- model label --------------------------------------------------------

    def test_model_matches_dashboard_rule(self):
        # Golden main chain: opus-4-8 x4, opus-5 x1, sonnet-4-6 x1; advisor
        # fable-5. The subagent copy is relabelled fable too, so a rule that let
        # sidechain or advisor turns vote would pick fable; the dashboard
        # (reconcile_session_models) ignores both and takes priority, then count.
        dst = self.sub()
        dst.write_text(dst.read_text(encoding="utf-8").replace(
            '"model": "claude-', '"model": "claude-fable-5-x-'), encoding="utf-8")
        self.assertEqual(api.get_live_session(session_id=SESSION)["model"],
                         "claude-opus-4-8")

    # -- resolution ---------------------------------------------------------

    def test_by_session_id_and_by_cwd_agree(self):
        a = api.get_live_session(session_id=SESSION)
        b = api.get_live_session(cwd=CWD)
        self.assertNotIn("error", a)
        self.assertEqual(a, b)
        self.assertEqual(a["session_id"], SESSION)
        self.assertEqual(a["display_id"], SESSION[:8])

    def test_cwd_slug_mangles_dots_and_underscores_too(self):
        """Claude Code replaces EVERY non-alphanumeric character with '-', not
        just the separators: C:\\dev\\.tools\\my_repo -> C--dev--tools-my-repo.
        A ':\\/'-only rule finds nothing for any path with a '.' or '_' in it -
        which is most of them."""
        dotted = "C:\\dev\\.tools\\my_repo"
        (self.tmp / "projects" / "C--dev--tools-my-repo").mkdir()
        shutil.copy(FIXTURE,
                    self.tmp / "projects" / "C--dev--tools-my-repo" / "s.jsonl")
        self.assertNotIn("error", api.get_live_session(cwd=dotted))

    def test_unknown_session_errors_rather_than_guessing(self):
        self.assertIn("error", api.get_live_session(session_id="ffffffff-0000-0000-0000-000000000000"))
        self.assertIn("error", api.get_live_session(cwd="C:\\nope"))
        self.assertIn("error", api.get_live_session())

    def test_session_id_must_be_a_uuid(self):
        # The id is interpolated into a filesystem glob, so the pattern - not the
        # filesystem - is what has to reject a traversal. Proven by planting the
        # target file exactly where the traversal would land: with the guard
        # removed, this glob resolves; with it, the id never reaches the glob.
        planted = self.tmp / "projects" / "escaped.jsonl"
        shutil.copy(FIXTURE, planted)
        self.assertIn("error", api.get_live_session(
            session_id="../../projects/escaped"))
        self.assertIn("error", api.get_live_session(session_id="../../../etc/passwd"))

    def test_extra_projects_dirs_from_the_env_are_searched(self):
        """config.claude_projects_dirs is how every default-path scan site picks
        up WHYTOKENS_EXTRA_PROJECTS_DIRS; a session the scanner ingests must not
        come back 'not found' here."""
        extra = self.tmp / "extra"
        (extra / SLUG).mkdir(parents=True)
        other = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
        shutil.copy(FIXTURE, extra / SLUG / (other + ".jsonl"))
        self.assertIn("error", api.get_live_session(session_id=other))
        os.environ["WHYTOKENS_EXTRA_PROJECTS_DIRS"] = str(extra)
        try:
            self.assertNotIn("error", api.get_live_session(session_id=other))
        finally:
            del os.environ["WHYTOKENS_EXTRA_PROJECTS_DIRS"]

    # -- the numbers --------------------------------------------------------

    def test_totals_match_a_direct_parse(self):
        _, turns, _ = scanner.parse_jsonl_file(str(self.main))
        live = api.get_live_session(session_id=SESSION)
        self.assertEqual(live["turns"], len(turns))
        for key, col in (("input", "input_tokens"), ("output", "output_tokens"),
                         ("cache_read", "cache_read_tokens"),
                         ("cache_creation", "cache_creation_tokens")):
            self.assertEqual(live[key], sum(t.get(col, 0) or 0 for t in turns), key)
        expected = sum(pricing.calc_cost(
            t["model"], t["input_tokens"], t["output_tokens"],
            t["cache_read_tokens"], t["cache_creation_tokens"], t["timestamp"],
            t.get("cache_creation_1h_tokens", 0))
            * pricing.price_mult(t.get("speed"), t.get("inference_geo")) for t in turns)
        self.assertAlmostEqual(live["cost"], round(expected, 4), places=4)
        self.assertGreater(live["cost"], 0)   # a silent 0.0 is the failure mode
        self.assertAlmostEqual(live["cost_per_turn"],
                               round(expected / len(turns), 4), places=4)

    def test_subagent_transcripts_are_counted(self):
        """Subagent records carry the PARENT sessionId and the scanner rolls them
        into the same session row - so the live read must include them too."""
        before = api.get_live_session(session_id=SESSION)
        self.sub()
        after = api.get_live_session(session_id=SESSION)
        self.assertEqual(after["turns"], before["turns"] * 2)
        self.assertGreater(after["cost"], before["cost"])
        # ...but a subagent's own context is not this session's replay cost.
        self.assertEqual(after["context_tokens"], before["context_tokens"])

    def test_context_is_the_newest_main_chain_turn(self):
        _, turns, _ = scanner.parse_jsonl_file(str(self.main))
        last = max((t for t in turns if not t.get("is_sidechain")),
                   key=lambda t: t["timestamp"])
        self.assertEqual(
            api.get_live_session(session_id=SESSION)["context_tokens"],
            last["input_tokens"] + last["cache_read_tokens"] + last["cache_creation_tokens"])

    def test_metadata_mirrors_the_dashboard_row(self):
        live = api.get_live_session(session_id=SESSION)
        metas, _, _ = scanner.parse_jsonl_file(str(self.main))
        self.assertEqual(live["project"], metas[0]["project_name"])
        self.assertEqual(live["branch"], metas[0]["git_branch"])
        self.assertGreaterEqual(live["duration_min"], 0)


class LiveRouteTest(unittest.TestCase):
    """GET /api/live over real HTTP. The function-level tests above cannot see a
    broken route (a query param read before parse_qs 500s the request while
    get_live_session itself stays perfectly correct)."""

    @classmethod
    def setUpClass(cls):
        from http.server import HTTPServer
        from api import DashboardHandler
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        proj = self.tmp / "projects" / SLUG
        proj.mkdir(parents=True)
        shutil.copy(FIXTURE, proj / (SESSION + ".jsonl"))
        self._saved = scanner.DEFAULT_PROJECTS_DIRS
        scanner.DEFAULT_PROJECTS_DIRS = [self.tmp / "projects"]

    def tearDown(self):
        scanner.DEFAULT_PROJECTS_DIRS = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, qs):
        url = "http://127.0.0.1:%d/api/live?%s" % (self.port, qs)
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_route_returns_the_same_payload_as_the_function(self):
        status, body = self.get("session_id=" + SESSION)
        self.assertEqual(status, 200)
        self.assertEqual(body, api.get_live_session(session_id=SESSION))
        self.assertGreater(body["cost"], 0)

    def test_route_accepts_cwd(self):
        status, body = self.get("cwd=" + urllib.parse.quote(CWD))
        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], SESSION)

    def test_route_reports_unknown_session_as_json_not_a_500(self):
        status, body = self.get("session_id=ffffffff-0000-0000-0000-000000000000")
        self.assertEqual(status, 200)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
