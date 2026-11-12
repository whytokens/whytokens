"""Trace deep link: state -> URL -> state round trip, run against the real module."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = (ROOT / "static/modules/tracelink.js").as_uri()
APP = (ROOT / "static/modules/app.js").read_text(encoding="utf-8")

HARNESS = """
import assert from 'node:assert/strict';
const { writeTraceLink, readTraceLink, findTurnRow } = await import(%r);
const state = { turn: 'msg_b', hl: new Set(['msg_a', 'msg_b']), why: 'Idle gaps (cache expiry): 41 min away, a, b & "c"' };
const p = new URLSearchParams({ tab: 'trace', session: 'S1' });
writeTraceLink(p, state);
const back = readTraceLink('?' + p.toString());
assert.equal(back.turn, state.turn);
assert.deepEqual([...back.hl], [...state.hl]);
assert.equal(back.why, state.why);
// empty state writes nothing and reads back empty
const e = new URLSearchParams({ tab: 'trace' });
writeTraceLink(e, { turn: '', hl: new Set(), why: '' });
assert.equal(e.toString(), 'tab=trace');
const none = readTraceLink('?tab=trace');
assert.equal(none.turn, ''); assert.equal(none.hl.size, 0); assert.equal(none.why, '');
// scroll target: known id -> its index, unknown/empty -> null (trace still renders)
const turns = [{ message_id: 'msg_a' }, { message_id: 'msg_b' }];
assert.equal(findTurnRow(turns, 'msg_b'), 1);
assert.equal(findTurnRow(turns, 'gone'), null);
assert.equal(findTurnRow(turns, ''), null);
""" % MOD


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestTraceLink(unittest.TestCase):
    def test_url_round_trip_and_unknown_turn(self):
        r = subprocess.run(["node", "--input-type=module", "-e", HARNESS], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_app_persists_and_restores_the_link(self):
        self.assertIn("writeTraceLink(params,", APP)  # updateURL survives reload
        self.assertIn("readTraceLink(window.location.search)", APP)  # first load restores it
        self.assertIn("scrollIntoView", APP)
        self.assertIn('id="trace-banner"', (ROOT / "static/index.html").read_text(encoding="utf-8"))
        self.assertIn("mine !== traceOpenSeq", APP)  # stale open is dropped
        self.assertIn("data-turn=", APP)  # per-row evidence link


if __name__ == "__main__":
    unittest.main()
