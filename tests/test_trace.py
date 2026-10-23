"""Trace view (reduced): get_session_detail exposes full timestamps, actor, estimated
flag, context size and idle gaps per turn. Run: python -m unittest discover -s tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import get_db, init_db  # noqa: E402
from api import get_session_detail  # noqa: E402

ROWS = [  # ts, agent_id, input, cache_read, cache_creation, estimated
    ("2026-10-08T10:00:00.123Z", None, 10, 1000, 200, 0),
    ("2026-10-08T10:00:05.000Z", "a1", 5, 300, 0, 1),
    ("2026-10-08T10:20:00.000Z", None, 12, 1500, 0, 0),
]


class TestTrace(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = Path(f.name)
        c = get_db(self.db)
        init_db(c)
        c.execute("INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp, turn_count)"
                  " VALUES ('s1','p','2026-10-08T10:00:00Z','2026-10-08T10:20:00Z',3)")
        c.execute("INSERT INTO agents (agent_id, agent_type) VALUES ('a1','Explore')")
        for i, (ts, aid, inp, cr, cc, est) in enumerate(ROWS):
            c.execute("INSERT INTO turns (session_id,timestamp,model,input_tokens,output_tokens,"
                      "cache_read_tokens,cache_creation_tokens,message_id,agent_id,estimated)"
                      " VALUES ('s1',?,'m',?,1,?,?,?,?,?)", (ts, inp, cr, cc, f"m{i}", aid, est))
        c.commit()
        c.close()
        self.turns = get_session_detail("s1", db_path=self.db)["turns"]

    def test_full_timestamp(self):
        self.assertEqual(self.turns[0]["ts"], "2026-10-08T10:00:00.123Z")

    def test_actor_and_estimated(self):
        self.assertEqual([t["actor"] for t in self.turns], ["main", "Explore", "main"])
        self.assertEqual([t["estimated"] for t in self.turns], [False, True, False])

    def test_context_is_prompt_size(self):
        self.assertEqual([t["context"] for t in self.turns], [1210, 305, 1512])

    def test_gap_is_per_actor_idle_time(self):
        # main: turn 3 follows turn 1 by ~20 min; the subagent turn in between does not reset it
        self.assertIsNone(self.turns[0]["gap_s"])
        self.assertIsNone(self.turns[1]["gap_s"])
        self.assertEqual(self.turns[2]["gap_s"], 1199.9)

    def test_actor_summary(self):
        a = {x["actor"]: x for x in get_session_detail("s1", db_path=self.db)["actors"]}
        self.assertEqual(a["main"]["turns"], 2)
        self.assertEqual(a["main"]["peak_context"], 1512)
        self.assertEqual(a["Explore"]["estimated_turns"], 1)


if __name__ == "__main__":
    unittest.main()
