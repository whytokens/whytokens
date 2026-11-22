"""One rule: a turn is priced by its own UTC date; display days only group.

Run in a subprocess pinned to New York time, because the bug needs a local day that
differs from the UTC day (01:00Z on Oct 7 is still Oct 6 in New York, one day before
the Sonnet 5.5 cache-read tier change).
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scanner import get_db, init_db
import api, pricing
db = Path(tempfile.mkdtemp()) / "u.db"
c = get_db(db); init_db(c)
c.execute("INSERT INTO sessions (session_id, project_name, last_timestamp, first_timestamp, model) VALUES ('s','p','2026-10-07T01:00:00Z','2026-10-06T10:00:00Z','claude-sonnet-5-5')")
for i, ts in enumerate(("2026-10-06T10:00:00Z", "2026-10-07T01:00:00Z")):
    c.execute("INSERT INTO turns (session_id, timestamp, model, cache_read_tokens, message_id) "
              "VALUES ('s',?,'claude-sonnet-5-5',1000000,?)", (ts, f"m{i}"))
c.commit(); c.close()
d = api.get_daily(db_path=db, tz="local")
dash = api.get_dashboard_data(db_path=db, tz="local")
print(json.dumps({"daily": d["totals"]["cost"], "days": [r["day"] for r in d["daily"]],
                  "bm": [m.get("timestamp") for s in dash["sessions_all"] for m in s["by_model"]],
                  "dbm": [r.get("rate_day") for r in dash["daily_by_model"]]}))
"""


class TestTierDay(unittest.TestCase):
    def test_turn_priced_by_own_utc_date_across_tier_change(self):
        env = dict(os.environ, TZ="EST5EDT")
        out = subprocess.run([sys.executable, "-I", "-c", SCRIPT, str(ROOT)], env=env,
                             capture_output=True, text=True, check=True).stdout
        r = json.loads(out.strip().splitlines()[-1])
        # Oct 6 turn at $0.20/MTok + Oct 7 turn at $0.10/MTok
        self.assertAlmostEqual(r["daily"], 0.30, places=6)
        self.assertEqual(r["days"], ["2026-10-06"])  # both local-Oct-6, grouped after pricing
        self.assertEqual(sorted(r["bm"]), ["0000-00-00", "2026-10-07"])
        self.assertEqual(sorted(r["dbm"]), ["0000-00-00", "2026-10-07"])


if __name__ == "__main__":
    unittest.main()
