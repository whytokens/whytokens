"""doctor MCP check: configured servers and duplicate installs, from fixture dirs only."""
import json
import tempfile
import unittest
from pathlib import Path

import mcp_config

SPEC = {"command": "npx", "args": ["-y", "@acme/browser-mcp"]}


class TestMcpConfig(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.proj = Path(tempfile.mkdtemp())

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_duplicate_install_across_user_and_plugin_flagged(self):
        self._write(self.home / ".claude.json", {"mcpServers": {"browser": SPEC, "other": {"command": "x"}}})
        self._write(self.home / ".claude" / "plugins" / "cache" / "acme" / ".mcp.json",
                    {"mcpServers": {"acme-browser": SPEC}})
        servers = mcp_config.configured_servers(self.home, self.proj)
        self.assertEqual(len(servers), 3)
        groups = mcp_config.duplicates(servers)
        self.assertEqual(groups, [[("plugin", "acme-browser"), ("user", "browser")]])
        self.assertIn("duplicate install: acme-browser [plugin] = browser [user]", mcp_config.format_report(servers))

    def test_distinct_servers_and_project_scope_are_silent(self):
        self._write(self.proj / ".mcp.json", {"mcpServers": {"a": SPEC, "b": {"url": "https://x/mcp"}}})
        servers = mcp_config.configured_servers(self.home, self.proj)
        self.assertEqual({s[0] for s in servers}, {"project"})
        self.assertEqual(mcp_config.duplicates(servers), [])
        self.assertIn("no duplicate servers", mcp_config.format_report(servers))

    def test_stale_cached_plugin_versions_are_not_a_duplicate(self):
        for v in ("1.0", "1.1"):
            self._write(self.home / ".claude" / "plugins" / "cache" / "acme" / v / ".mcp.json",
                        {"mcpServers": {"browser": SPEC}})
        self.assertEqual(mcp_config.duplicates(mcp_config.configured_servers(self.home, self.proj)), [])

    def test_missing_or_corrupt_config_is_empty(self):
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(mcp_config.configured_servers(self.home, self.proj), [])


if __name__ == "__main__":
    unittest.main()
