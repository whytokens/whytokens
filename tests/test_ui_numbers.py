"""Numbers that reach innerHTML are numbers, and every priced model classifies as billable.
Pure helpers run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FMT = (ROOT / "static" / "modules" / "format.js").as_uri()
PRC = (ROOT / "static" / "modules" / "pricing.js").as_uri()


def node(code):
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestUiNumbers(unittest.TestCase):
    def test_formatters_never_return_raw_strings(self):
        evil = "<img src=x onerror=alert(1)>"
        r = node(f"import * as f from {json.dumps(FMT)}; const e = {json.dumps(evil)};"
                 "console.log(JSON.stringify([f.fmt(e), f.fmtCost(e), f.fmtCostBig(e), f.fmtPct(e), f.fmt(1500), f.fmt('2500')]));")
        for v in r[:4]:
            self.assertNotIn("<", v)
        self.assertEqual(r[4:], ["1.5K", "2.5K"])

    def test_mythos_is_billable(self):
        r = node(f"import * as p from {json.dumps(PRC)};"
                 "console.log(JSON.stringify([p.isBillable('claude-mythos-5-2'), p.isBillable('qwen-local')]));")
        self.assertEqual(r, [True, False])


class TestHourlyOffset(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_hour_converts_with_the_events_own_date(self):
        import os
        src = (ROOT / "static" / "modules" / "app.js").read_text(encoding="utf-8")
        fns = src[src.index("function localOffsetHours("):src.index("function isPeakHour(")]
        code = fns + "console.log(JSON.stringify([utcHourToDisplay(16, 'local', '2026-01-15'), "                      "utcHourToDisplay(16, 'local', '2026-07-01'), utcHourToDisplay(16, 'utc', '2026-07-01')]));"
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, TZ="America/New_York"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [11, 12, 16])  # winter EST, summer EDT, UTC untouched


class TestDispatchNumbers(unittest.TestCase):
    def test_non_numeric_dispatch_fields_never_reach_the_payload(self):
        import tempfile
        import api
        from scanner import get_db, init_db
        db = Path(tempfile.mkdtemp()) / "u.db"
        c = get_db(db)
        init_db(c)
        c.execute("INSERT INTO agents (agent_id, agent_type, total_tokens, total_duration_ms, tool_use_count) "
                  "VALUES ('a', 't', '<img src=x onerror=alert(1)>', 'x', '<b>')")
        c.commit()
        c.close()
        d = api.get_dashboard_data(db_path=db)["subagent_dispatches"][0]
        self.assertEqual((d["total_tokens"], d["total_duration_ms"], d["tool_use_count"]), (0, 0, 0))

    def test_infinite_dispatch_count_does_not_break_the_response(self):
        import tempfile
        import api
        from scanner import get_db, init_db
        db = Path(tempfile.mkdtemp()) / "u.db"
        c = get_db(db)
        init_db(c)
        c.execute("INSERT INTO agents (agent_id, agent_type, total_tokens) VALUES ('a', 't', 1e999)")
        c.commit()
        c.close()
        self.assertEqual(api.get_dashboard_data(db_path=db)["subagent_dispatches"][0]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
