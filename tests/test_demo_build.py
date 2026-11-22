"""The demo is built only from the synthetic corpus, and the first screen is wired in.

The leak check is a verifier, so it is tried on mutants: a real-looking name, a home path, a tool
input must each turn it red (test_leak_check_catches_*)."""
import getpass
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_demo  # noqa: E402
import demo_corpus  # noqa: E402

NAME_KEYS = {"project": set(demo_corpus.PROJECTS) | {""}, "projects": set(demo_corpus.PROJECTS),
             "project_name": set(demo_corpus.PROJECTS), "cwd": {"/work/" + p for p in demo_corpus.PROJECTS},
             "topic": {t for ts in demo_corpus.TITLES.values() for t in ts},
             "ai_title": {t for ts in demo_corpus.TITLES.values() for t in ts}}
# What a tool call's input may say: the corpus writes exactly these.
TOOL_INPUTS = {"file_path": re.compile(r"/work/[\w/]+/src/file\d\.py"), "file": re.compile(r"file\d\.py"),
               "command": re.compile(r"make test")}


def demo_data(html):
    return json.loads(re.search(r"var DATA = (.*);\n  function key", html).group(1))


def strings(o, key=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from strings(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v, key)
    elif isinstance(o, str):
        yield key, o


def leaks(html):
    """Problems found in a built demo; empty means only the corpus's own names are in it."""
    bad = []
    data = demo_data(html)
    home, tmp = str(Path.home()), tempfile.gettempdir()
    # Decoded values and keys, plus the page text in every spelling a path can take (JSON-escaped too).
    texts = [html] + [k for k, _ in strings(data)] + [v for _, v in strings(data)] + list(keys(data))
    for mine in (home, tmp, str(ROOT)):
        for form in {mine, mine.replace("\\", "/"), json.dumps(mine)[1:-1]}:
            if any(form in t for t in texts):
                bad.append(f"machine path in page: {form!r}")
    for key, val in strings(data):
        if key in NAME_KEYS and val not in NAME_KEYS[key]:
            bad.append(f"{key}={val!r} is not a corpus name")
        if key in TOOL_INPUTS and not TOOL_INPUTS[key].fullmatch(val):
            bad.append(f"tool input {key}={val!r} is not the corpus's")
        if key == "path" and val != "(synthetic corpus)":
            bad.append(f"path={val!r}")
    for k in keys(data):  # a name used as a map key (per-project / per-session maps)
        if "/" in k and not k.startswith("/api/") and k not in NAME_KEYS["project"]:
            bad.append(f"map key {k!r} is not a corpus name")
    return bad


def keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from keys(v)


class DemoBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo.html"
            build_demo.build(out)
            cls.html = out.read_text(encoding="utf-8")

    def test_self_contained(self):
        self.assertNotRegex(self.html, r'(?:src|href)="/static/')  # nothing fetched: it opens from file://
        self.assertNotRegex(self.html, r"from '\./\w+\.js'")  # no module imports left in the bundle
        self.assertIn("demo-banner", self.html)

    def test_only_corpus_names(self):
        self.assertEqual(leaks(self.html), [])
        projects = {v for k, v in strings(demo_data(self.html)) if k == "project"}
        self.assertTrue(projects >= set(demo_corpus.PROJECTS), "the corpus's projects must be in the demo")

    def test_every_listed_session_opens_a_trace(self):
        d = demo_data(self.html)
        for tz in ("local", "utc"):
            for x in d[f"/api/data?tz={tz}"]["sessions_all"]:
                self.assertIn(f"/api/session?session_id={x['full_session_id']}", d)

    def test_journey_data_present(self):
        d = demo_data(self.html)
        self.assertTrue(d["/api/waste"]["findings"])
        self.assertTrue(any(k.startswith("/api/waste?session_id=") for k in d))
        self.assertEqual([c["state"] for c in d["/api/changes"]["changes"]], ["reduced"])
        self.assertEqual(d["/api/changes"]["changes"][0]["fix"]["project"], "demo/docs")

    def test_leak_check_catches_real_names(self):
        base = self.html
        swap = lambda a, b: base.replace(a, b, 1)
        self.assertTrue(leaks(swap('"project": "acme/web"', '"project": "secret-client/app"')))
        self.assertTrue(leaks(swap('"cwd": "/work/acme/web"', '"cwd": "C:\\\\Work\\\\secret"')))
        self.assertTrue(leaks(swap('"topic": "Add dark mode toggle"', '"topic": "Fix Acme payroll bug"')))
        self.assertTrue(leaks(swap('"file_path": "/work/acme/web/src/file0.py"', '"file_path": "/srv/secret/id_rsa"')))
        self.assertTrue(leaks(swap("(synthetic corpus)", json.dumps(str(Path.home()))[1:-1])))
        self.assertTrue(leaks(swap("(synthetic corpus)", json.dumps(tempfile.gettempdir())[1:-1] + "x")))  # escaped temp path
        self.assertTrue(leaks(swap('"/api/provenance": {', '"/api/provenance": {"secret-client/app": 1, ')))  # a private name as a map key

    def test_clean_page_is_green_and_scrub_is_needed(self):
        self.assertEqual(leaks(self.html), [])
        self.assertEqual(build_demo.scrub({"a": [{"path": r"C:\Users\x"}]}), {"a": [{"path": "(synthetic corpus)"}]})


class FirstScreen(unittest.TestCase):
    def test_wired_in(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "static" / "modules" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="first-screen"', html)
        self.assertLess(html.index('id="first-screen"'), html.index('id="stats-row"'))
        self.assertIn("renderFirstScreen()", app)
        self.assertIn("data-fs-dismiss", (ROOT / "static" / "modules" / "firstscreen.js").read_text(encoding="utf-8"))
        self.assertIn("data-fs-show", app)  # hiding it leaves a way back


if __name__ == "__main__":
    unittest.main()
