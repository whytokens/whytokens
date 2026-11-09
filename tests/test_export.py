import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import api
import cli
from tests.test_dashboard import _seed_multi


class TestExport(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = Path(f.name)
        _seed_multi(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def test_document_shape_and_version(self):
        doc = api.get_export(self.db)
        self.assertEqual(doc["schema_version"], 1)  # pins the contract: bump deliberately, update docs/SCHEMA.md
        self.assertEqual(set(doc), {"schema_version", "totals", "sessions", "waste"})
        self.assertEqual(doc["totals"]["sessions"], 3)
        self.assertEqual(doc["totals"]["output"], sum(s["output"] for s in doc["sessions"]))
        self.assertIn("findings", doc["waste"])

    def test_cli_emits_json(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "DB_PATH", self.db), redirect_stdout(buf):
            cli.cmd_export(as_json=True)
        self.assertEqual(json.loads(buf.getvalue())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
