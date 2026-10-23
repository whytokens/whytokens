"""Coordinator spend source (sources/coordinator.py). Synthetic blobs only: no test
reads the real desktop cache."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources  # noqa: E402
from sources.coordinator import CoordinatorSource, parse_value, PROJECT  # noqa: E402
from tests.test_project_threads import blob  # noqa: E402


def mu(model, inp=0, out=0, cr=0, cw=0, canonical=None):
    return {model: {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": cr,
                    "cacheCreationInputTokens": cw, "canonicalModel": canonical or model}}


def result(uuid, ts, usage, model_usage):
    return {"type": "result", "uuid": uuid, "created_at": ts, "usage": usage,
            "modelUsage": model_usage}


def usage(inp=0, out=0, cr=0, cw5=0, cw1=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw5 + cw1, "speed": "standard",
            "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1}}


def conv(*events, cid="cse_X"):
    return {"conversationUuid": "code:" + cid, "tree": {"events": list(events)}}


# A mid-stream assistant snapshot: its output is far below the result's final tally.
SNAPSHOT = {"type": "assistant", "message": {"id": "msg_1", "role": "assistant", "model": "claude-opus-5-5",
            "usage": {"input_tokens": 1, "output_tokens": 3, "cache_read_input_tokens": 100}}}

R1 = result("r1", "2026-10-10T02:22:47.998609Z", usage(2, 40, 100, 0, 30),
            {**mu("claude-opus-5-5[1m]", 2, 40, 100, 30, "claude-opus-5-5"), **mu("claude-haiku-5-5", 9, 1)})
R2 = result("r2", "2026-10-10T02:30:00.000000Z", usage(3, 50, 200, 5, 10),
            {**mu("claude-opus-5-5[1m]", 5, 90, 300, 45, "claude-opus-5-5"), **mu("claude-haiku-5-5", 20, 4)})


class ParseTest(unittest.TestCase):
    def test_result_usage_beats_stream_snapshot(self):
        _, turns = parse_value(conv(SNAPSHOT, R1))
        main = [t for t in turns if t["message_id"] == "r1"]
        self.assertEqual(len(main), 1)
        self.assertEqual((main[0]["output_tokens"], main[0]["cache_read_tokens"]), (40, 100))
        self.assertNotIn("msg_1", {t["message_id"] for t in turns})

    def test_cache_write_split_and_model(self):
        _, turns = parse_value(conv(R1, R2))
        t = next(t for t in turns if t["message_id"] == "r2")
        self.assertEqual((t["cache_creation_tokens"], t["cache_creation_5m_tokens"],
                          t["cache_creation_1h_tokens"]), (15, 5, 10))
        self.assertEqual(t["model"], "claude-opus-5-5")
        self.assertEqual(t["timestamp"], "2026-10-10T02:30:00.000Z")

    def test_side_model_is_delta(self):
        _, turns = parse_value(conv(R2, R1))  # out of order: sorted by created_at
        side = [t for t in turns if t["model"] == "claude-haiku-5-5"]
        self.assertEqual([(t["message_id"], t["input_tokens"], t["output_tokens"]) for t in side],
                         [("r2:claude-haiku-5-5", 11, 3)])
        # Without a confirmed base, the side share is unknown and skipped.
        _, turns = parse_value(conv(R2))
        self.assertEqual([t["message_id"] for t in turns], ["r2"])

    def test_main_model_picked_per_query(self):
        # The conversation switches to Fable: r2's cache read is all Fable's.
        r1 = result("r1", "2026-10-10T01:00:00Z", usage(1, 1, 500), mu("claude-opus-5-5", 1, 1, 500))
        r2 = result("r2", "2026-10-10T02:00:00Z", usage(1, 2, 80),
                    {**mu("claude-opus-5-5", 1, 1, 500), **mu("claude-fable-5-1", 1, 2, 80)})
        _, turns = parse_value(conv(r1, r2))
        self.assertEqual([(t["message_id"], t["model"], t["cache_read_tokens"]) for t in turns],
                         [("r1", "claude-opus-5-5", 500), ("r2", "claude-fable-5-1", 80)])

    def test_uncached_side_model_does_not_take_main_usage(self):
        # No cache reads: Haiku's larger output must not make it the main model.
        r1 = result("r1", "2026-10-10T01:00:00Z", usage(10, 1), {**mu("o", 10, 1), **mu("h", 0, 0)})
        r2 = result("r2", "2026-10-10T02:00:00Z", usage(8000, 5), {**mu("o", 8010, 6), **mu("h", 100, 50)})
        _, turns = parse_value(conv(r1, r2))
        self.assertEqual([(t["message_id"], t["model"], t["input_tokens"]) for t in turns],
                         [("r1", "o", 10), ("r2", "o", 8000), ("r2:h", "h", 100)])
        self.assertEqual(sum(t["input_tokens"] for t in turns), 8110)

    def test_exact_match_beats_largest_delta_and_no_match_emits_no_sides(self):
        r1 = result("r1", "2026-10-10T01:00:00Z", usage(10, 1), {**mu("o", 10, 1), **mu("h", 0, 0)})
        # Haiku's side call has the larger cache read; Opus's delta is the usage.
        r2 = result("r2", "2026-10-10T02:00:00Z", usage(8000, 5),
                    {**mu("o", 8010, 6), **mu("h", 0, 1, 300)})
        # A counter reset: no delta equals the usage, so nothing is confirmed.
        r3 = result("r3", "2026-10-10T03:00:00Z", usage(7, 7), {**mu("o", 9000, 10), **mu("h", 0, 9, 900)})
        _, turns = parse_value(conv(r1, r2, r3))
        self.assertEqual([(t["message_id"], t["model"], t["estimated"]) for t in turns if ":" not in t["message_id"]],
                         [("r1", "o", 1), ("r2", "o", 0), ("r3", "h", 1)])
        self.assertEqual([(t["message_id"], t["cache_read_tokens"]) for t in turns if ":" in t["message_id"]],
                         [("r2:h", 300)])

    def test_unconfirmed_model_is_estimated(self):
        _, turns = parse_value(conv(R2))  # no base and cumulative != own usage
        self.assertEqual([(t["message_id"], t["estimated"]) for t in turns], [("r2", 1)])
        _, turns = parse_value(conv(R1, R2))
        self.assertEqual({t["message_id"]: t.get("estimated") for t in turns if ":" not in t["message_id"]},
                         {"r1": 1, "r2": 0})

    def test_first_model_query_without_base_is_not_confirmed(self):
        # Fable's cumulative equals r2's usage only because it is Fable's first query;
        # Opus's cumulative is r1's history and must not become a side row of r2.
        r2 = result("r2", "2026-10-10T02:00:00Z", usage(1, 2, 80),
                    {**mu("claude-opus-5-5", 1, 1, 500), **mu("claude-fable-5-1", 1, 2, 80)})
        _, turns = parse_value(conv(r2))
        self.assertEqual([(t["message_id"], t["estimated"], t["cache_read_tokens"]) for t in turns],
                         [("r2", 1, 80)])

    def test_non_code_value_ignored(self):
        self.assertEqual(parse_value({"conversationUuid": "chat:x", "tree": {"e": [R1]}}), (None, []))
        self.assertEqual(parse_value(conv(SNAPSHOT)), (None, []))

    def test_malformed_fields_are_dropped_not_fatal(self):
        bad = result("r1", "2026-10-10T01:00:00Z",
                     dict(usage(2, 3, 4), speed={"x": 1}, inference_geo=7, input_tokens="9"),
                     mu("claude-opus-5-5", 9, 3, 4))
        _, turns = parse_value(conv(bad))
        self.assertEqual([(t["speed"], t["inference_geo"], t["input_tokens"], t["output_tokens"]) for t in turns],
                         [("", "", 0, 3)])


class ScanTest(unittest.TestCase):
    def _scan(self, root, db):
        sources.scan_all(db_path=db, verbose=False,
                         sources=[CoordinatorSource(blob_dir=str(root))])

    def test_later_blob_corrects_guessed_model(self):
        # A window holding only the post-switch query guesses Opus; a later blob with
        # its predecessor proves Fable, and the confirmed row must replace the guess.
        r1 = result("r1", "2026-10-10T01:00:00Z", usage(1, 1, 500), mu("claude-opus-5-5", 1, 1, 500))
        r2 = result("r2", "2026-10-10T02:00:00Z", usage(1, 2, 80),
                    {**mu("claude-opus-5-5", 1, 1, 500), **mu("claude-fable-5-1", 1, 2, 80)})
        with tempfile.TemporaryDirectory() as d:
            root, db = Path(d) / "blobs", Path(d) / "u.db"
            root.mkdir()
            def rows():
                c = sqlite3.connect(db)
                try:
                    return (c.execute("SELECT message_id, model, estimated, cache_read_tokens FROM turns "
                                      "ORDER BY message_id").fetchall(),
                            c.execute("SELECT total_cache_read, turn_count FROM sessions").fetchone())
                finally:
                    c.close()

            (root / "1").write_bytes(blob(conv(r2)))
            self._scan(root, db)
            self.assertEqual(rows(), ([("r2", "claude-opus-5-5", 1, 80)], (80, 1)))  # guessed
            (root / "2").write_bytes(blob(conv(r1, r2)))
            self._scan(root, db)
            self.assertEqual(rows(), ([("r1", "claude-opus-5-5", 1, 500),
                                       ("r2", "claude-fable-5-1", 0, 80)], (580, 2)))

    def test_scan_writes_partial_sessions_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "blobs" / "5" / "00").mkdir(parents=True)
            (tmp / "blobs" / "5" / "00" / "8").write_bytes(blob(conv(R1, R2)))
            (tmp / "blobs" / "5" / "00" / "9").write_bytes(bytes([0xFF, 0x0F, 0xFF, 0x0F]))  # truncated: never fatal
            db = tmp / "u.db"
            for _ in range(2):
                sources.scan_all(db_path=db, verbose=False,
                                 sources=[CoordinatorSource(blob_dir=str(tmp / "blobs"))])
            # A newer blob of the same conversation (window slid) adds nothing twice.
            (tmp / "blobs" / "5" / "00" / "a").write_bytes(blob(conv(R2)))
            sources.scan_all(db_path=db, verbose=False,
                             sources=[CoordinatorSource(blob_dir=str(tmp / "blobs"))])
            c = sqlite3.connect(db)
            try:
                s = c.execute("SELECT agent, project_name, total_output_tokens, turn_count "
                              "FROM sessions WHERE session_id='cse_X'").fetchone()
                n = c.execute("SELECT COUNT(*) FROM turns WHERE agent='coordinator'").fetchone()[0]
            finally:
                c.close()
            self.assertEqual(s, ("coordinator", PROJECT, 40 + 50 + 3, 3))
            self.assertEqual(n, 3)

    def test_newest_blob_first_keeps_true_start(self):
        with tempfile.TemporaryDirectory() as d:
            root, db = Path(d) / "blobs", Path(d) / "u.db"
            root.mkdir()
            (root / "1").write_bytes(blob(conv(R2)))        # newer window, read first
            (root / "2").write_bytes(blob(conv(R1, R2)))
            self._scan(root, db)
            c = sqlite3.connect(db)
            try:
                row = c.execute("SELECT first_timestamp, last_timestamp FROM sessions "
                                "WHERE session_id='cse_X'").fetchone()
            finally:
                c.close()
            self.assertEqual(row, ("2026-10-10T02:22:47.998Z", "2026-10-10T02:30:00.000Z"))


if __name__ == "__main__":
    unittest.main()
