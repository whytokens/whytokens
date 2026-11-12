import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import version
from tests.test_server_hardening import Base

ROOT = Path(__file__).resolve().parent.parent


class TestVersion(unittest.TestCase):
    def test_checkout_pyproject_beats_installed_metadata(self):
        with mock.patch("importlib.metadata.version", return_value="0.0.0-stale"):
            want = next(l for l in (ROOT / "pyproject.toml").read_text().splitlines()
                        if l.startswith("version")).split('"')[1]
            self.assertEqual(version.build_info(ROOT)["version"], want)

    def test_no_git_dir_commit_null_and_no_raise(self):
        with tempfile.TemporaryDirectory() as d:
            info = version.build_info(Path(d))
        self.assertIsNone(info["commit"])
        self.assertTrue(info["started_at"])

    def test_cli_version_flag(self):
        out = subprocess.run([sys.executable, str(ROOT / "cli.py"), "--version"],
                             capture_output=True, text=True, cwd=ROOT).stdout
        self.assertTrue(out.startswith("whytokens " + version.INFO["version"]))


class TestEndpoint(Base):
    def test_endpoint_and_footer(self):
        st, body = self.req("GET", "/api/version", None)
        self.assertEqual(st, 200)
        self.assertEqual(set(json.loads(body)), {"version", "commit", "started_at"})
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "modules" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="build-info"', html)
        self.assertIn("getElementById('build-info')", js)
