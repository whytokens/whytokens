"""trace request bands + waterfall / heatmap: the API exposes prompt_seq (NULL stays
null), and the pure helpers in static/modules/analytics.js, run under node, behave.
Run: python -m unittest discover -s tests"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner import get_db, init_db  # noqa: E402
from api import get_session_detail  # noqa: E402

MODULE = (ROOT / "static" / "modules" / "analytics.js").as_uri()
APP_JS = (ROOT / "static" / "modules" / "app.js").read_text(encoding="utf-8")


def node(expr):
    code = f"import * as a from {json.dumps(MODULE)}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class TestApiPromptSeq(unittest.TestCase):
    def test_prompt_seq_passes_through_and_null_stays_null(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = Path(f.name)
        c = get_db(db)
        init_db(c)
        c.execute("INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp, turn_count)"
                  " VALUES ('s1','p','2026-10-08T10:00:00Z','2026-10-08T10:20:00Z',3)")
        for i, seq in enumerate((0, 2, None)):
            c.execute("INSERT INTO turns (session_id,timestamp,model,input_tokens,output_tokens,message_id,prompt_seq)"
                      " VALUES ('s1',?,'m',1,1,?,?)", (f"2026-10-08T10:0{i}:00Z", f"m{i}", seq))
        c.commit()
        c.close()
        self.assertEqual([t["prompt_seq"] for t in get_session_detail("s1", db_path=db)["turns"]], [0, 2, None])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestHelpers(unittest.TestCase):
    def test_bands_group_by_request_and_skip_unknown(self):
        turns = [{"prompt_seq": 0, "c": 9}, {"prompt_seq": 1, "c": 1}, {"prompt_seq": 1, "c": 2},
                 {"prompt_seq": 2, "c": 4}, {"prompt_seq": None, "c": 50}]
        self.assertEqual(node(f"a.promptBands({json.dumps(turns)}, t => t.c)"),
                         [{"seq": 1, "calls": 2, "cost": 3}, {"seq": 2, "calls": 1, "cost": 4}])

    def test_no_prompt_seq_means_no_bands(self):
        self.assertEqual(node("a.promptBands([{prompt_seq: null}, {}], () => 1)"), [])

    def test_band_filter_keeps_original_indexes(self):
        turns = [{"prompt_seq": 1}, {"prompt_seq": 2}, {"prompt_seq": 2}]
        got = node(f"a.turnsOfBand({json.dumps(turns)}, 2).map(x => x.i)")
        self.assertEqual(got, [1, 2])
        self.assertEqual(node(f"a.turnsOfBand({json.dumps(turns)}, null).length"), 3)

    def test_waterfall_steps_and_clamp(self):
        f = [{"session_id": "s", "cause": "idle_gap", "usd": 1, "session_usd": 10},
             {"session_id": "s", "cause": "repeated_reads", "usd": 3, "session_usd": 10},
             {"session_id": "x", "cause": "idle_gap", "usd": 9, "session_usd": 9}]
        wf = node(f"a.sessionWaterfall({json.dumps(f)}, 's')")
        self.assertEqual([s["cause"] for s in wf["steps"]], ["repeated_reads", "idle_gap"])
        self.assertEqual((wf["total"], wf["attributed"], wf["unattributed"], wf["over"]), (10, 4, 6, 0))
        f[0]["usd"] = 20
        wf = node(f"a.sessionWaterfall({json.dumps(f)}, 's')")
        self.assertEqual((wf["unattributed"], wf["over"]), (0, 13))
        self.assertIsNone(node(f"a.sessionWaterfall({json.dumps(f)}, 'none')"))
        wf = node(f"a.sessionWaterfall({json.dumps(f)}, 'none', 7)")  # no findings: all unattributed
        self.assertEqual((wf["total"], wf["steps"], wf["unattributed"]), (7, [], 7))

    def test_heatmap_matrix(self):
        sessions = [{"full_session_id": "a", "last_date": "2026-10-07"},   # Wed -> week of 10-05
                    {"full_session_id": "b", "last_date": "2026-10-14"}]   # week of 10-12
        f = [{"session_id": "a", "cause": "idle_gap", "usd": 1}, {"session_id": "a", "cause": "idle_gap", "usd": 2},
             {"session_id": "b", "cause": "idle_gap", "usd": 5}, {"session_id": "b", "cause": "resident_output", "usd": 1},
             {"session_id": "out", "cause": "idle_gap", "usd": 99}]
        m = node(f"a.causeWeekMatrix({json.dumps(f)}, {json.dumps(sessions)})")
        self.assertEqual(m["weeks"], ["2026-10-05", "2026-10-12"])
        self.assertEqual([(r["cause"], r["cells"]) for r in m["rows"]],
                         [("idle_gap", [3, 5]), ("resident_output", [0, 1])])
        self.assertEqual(m["max"], 5)


class TestWiring(unittest.TestCase):
    def test_app_uses_the_helpers_and_exposes_handlers(self):
        for needle in ("promptBands(", "turnsOfBand(", "sessionWaterfall(", "causeWeekMatrix(", "setTraceBand", "setWasteCell"):
            self.assertIn(needle, APP_JS)
        self.assertIn("from './analytics.js'", APP_JS)
        self.assertIn("if (wasteWeek && wasteCause && !v.cause) v.groups = [];", APP_JS)  # empty cell stays empty


if __name__ == "__main__":
    unittest.main()
