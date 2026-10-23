"""pyproject must list every top-level module and data dir, or an installed build breaks."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PackagingTest(unittest.TestCase):
    def setUp(self):
        self.toml = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_every_top_level_module_is_listed(self):
        listed = set(re.findall(r'"(\w+)"', re.search(r"py-modules = \[(.*?)\]", self.toml).group(1)))
        on_disk = {p.stem for p in ROOT.glob("*.py")} - {"setup"}
        self.assertEqual(on_disk, listed)

    def test_every_package_dir_is_listed(self):
        for d in ("sources", "static", "static/modules", "static/vendor", "resources"):
            self.assertIn('"%s"' % d.replace("/", "."), self.toml)


if __name__ == "__main__":
    unittest.main()
