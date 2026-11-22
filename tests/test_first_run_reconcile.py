"""Opening reconcile before the first scan answers cleanly and creates no database."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import api


class TestReconcileBeforeFirstScan(unittest.TestCase):
    def test_get_returns_error_and_creates_no_db(self):
        db = Path(tempfile.mkdtemp()) / "missing.db"
        server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(api, "DB_PATH", db):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/reconcile", timeout=5)
            body = json.loads(cm.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
        self.assertIn("scan", body["error"])
        self.assertFalse(db.exists())

    def test_read_connection_never_creates_the_file(self):
        db = Path(tempfile.mkdtemp()) / "missing.db"
        with self.assertRaises(Exception):
            api._connect(db)
        self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
