"""Projects thread titles: desktop-cache decode + join to local transcripts.

Builds synthetic IndexedDB blobs (Blink wrapper + snappy + V8) so no test reads the
real %APPDATA% cache.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_threads as pt  # noqa: E402
import scanner  # noqa: E402


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def v8(x):
    if x is None:
        return b"0"
    if x is True:
        return b"T"
    if x is False:
        return b"F"
    if isinstance(x, int):
        return b"I" + _varint((x << 1) ^ (x >> 63))
    if isinstance(x, str):
        try:
            raw = x.encode("latin-1")
            return b'"' + _varint(len(raw)) + raw
        except UnicodeEncodeError:
            raw = x.encode("utf-16-le")
            return b"c" + _varint(len(raw)) + raw
    if isinstance(x, list):
        return b"A" + _varint(len(x)) + b"".join(v8(i) for i in x) + b"$" + _varint(0) + _varint(len(x))
    if isinstance(x, dict):
        return b"o" + b"".join(v8(k) + v8(v) for k, v in x.items()) + b"{" + _varint(len(x))
    raise TypeError(x)


def snappy_literal(data):
    out = bytearray(_varint(len(data)))
    for i in range(0, len(data), 60):
        chunk = data[i:i + 60]
        out.append((len(chunk) - 1) << 2)
        out += chunk
    return bytes(out)


def blob(root):
    inner = b"\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0f" + v8(root)
    return b"\xff\x11\x02" + snappy_literal(inner)


def spawn(tool_id, title, message_id=None, instructions=None, session_id=None, thread_id=None):
    inp = {"title": title}
    if message_id:
        inp["message_id"] = message_id
    if instructions:
        inp["instructions"] = instructions
    events = [{"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "mcp__hearthbot__start_rc_session", "input": inp}]}}]
    if session_id or thread_id:
        res = {"status": "started", "session_id": session_id, "thread_id": thread_id}
        events.append({"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": [{"type": "text", "text": json.dumps(res)}]}]}})
    return events


def coordinator(*events):
    return {"conversationUuid": "code:cse_coord", "tree": {"messages": [e for ev in events for e in ev]}}


def wake_line(cmsg, body, cwd="C:\\repo"):
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content":
        '<wake reason="mention"><project id="chan_1" type="project">'
        '<message trigger="true" id="%s">%s</message></project></wake>' % (cmsg, body)}})


def relay_line(note):
    return json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content":
        '<relay from="coordinator" session="session_x" reason="spawn">\n  <note>\n      %s\n  </note>\n</relay>'
        % note.replace("'", "&#39;")}})


class SnappyV8Test(unittest.TestCase):
    def test_overlapping_copy(self):
        # literal "ab" then a 6-byte copy at offset 2 -> "abababab"
        self.assertEqual(pt.unsnappy(bytes([8, 4]) + b"ab" + bytes([9, 2])), b"abababab")

    def test_bad_offset_raises(self):
        with self.assertRaises(ValueError):
            pt.unsnappy(bytes([8, 4]) + b"ab" + bytes([9, 9]))
        # declared length matches what an unchecked (wrapping) slice would give
        with self.assertRaises(ValueError):
            pt.unsnappy(bytes([3, 4]) + b"ab" + bytes([9, 9]))

    def test_roundtrip(self):
        root = {"a": [1, -3, "x", None, True, {"n": "é"}], "u": "Ω two-byte"}
        self.assertEqual(pt.decode_blob(blob(root)), root)


class ThreadTitlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.blobs = self.tmp / "blob" / "5" / "00"
        self.blobs.mkdir(parents=True)
        self.proj = self.tmp / "projects"
        self.proj.mkdir()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        scanner.init_db(self.conn)

    def session(self, sid, lines, topic=None, source=None):
        p = self.proj / (sid + ".jsonl")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.conn.execute("INSERT INTO sessions (session_id, topic, topic_source) VALUES (?, ?, ?)",
                          (sid, topic, source))
        return str(p)

    def cache(self, name, root):
        (self.blobs / name).write_bytes(blob(root))

    def run_apply(self, files):
        return pt.apply_thread_titles(self.conn, files, self.tmp / "blob")

    def topic(self, sid):
        r = self.conn.execute("SELECT topic, topic_source FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return (r["topic"], r["topic_source"])

    def test_join_paths(self):
        self.cache("a", coordinator(
            spawn("t1", "By message id", message_id="cmsg_A"),
            spawn("t2", "By session id", session_id="cse_B"),
            spawn("t3", "By spawn note", instructions="Do the thing; don't\n   push."),
        ))
        files = [
            self.session("s1", [wake_line("cmsg_A", "hello")]),
            self.session("s2", [wake_line("cmsg_other", "hi", cwd="C:\\r\\.claude\\worktrees\\bridge-cse_B")]),
            self.session("s3", [relay_line("Do the thing; don't push.")]),
        ]
        self.assertEqual(self.run_apply(files), 3)
        self.assertEqual(self.topic("s1"), ("By message id", "thread"))
        self.assertEqual(self.topic("s2"), ("By session id", "thread"))
        self.assertEqual(self.topic("s3"), ("By spawn note", "thread"))

    def test_link_fallback_and_spawn_wins(self):
        post = {"type": "tool_use", "id": "p", "name": "mcp__hearthbot__post_message",
                "input": {"text": "Done in [Linked title](#cmsg_L) and [Anchor](#cmsg_S)."}}
        self.cache("a", coordinator([{"message": {"content": [post]}}],
                                    spawn("t", "Spawn title", message_id="cmsg_S")))
        files = [self.session("l", [wake_line("cmsg_L", "x")]),
                 self.session("s", [wake_line("cmsg_S", "x")])]
        self.run_apply(files)
        self.assertEqual(self.topic("l"), ("Linked title", "thread"))
        self.assertEqual(self.topic("s"), ("Spawn title", "thread"))

    def test_precedence(self):
        self.cache("a", coordinator(spawn("t", "Thread title", message_id="cmsg_A")))
        files = [
            self.session("custom", [wake_line("cmsg_A", "x")], "Mine", "custom"),
            self.session("ai", [wake_line("cmsg_A", "x")], "AI guess", "ai"),
            self.session("ai2", [wake_line("cmsg_none", "prompt text")], "AI guess", "ai"),
            self.session("bare", [wake_line("cmsg_none", "  first &amp; <b>only</b>  ")]),
            self.session("plain", [json.dumps({"type": "user", "message": {"content": "not a thread"}})]),
            self.session("blocks", [json.dumps({"type": "user", "message": {"content": [{"type": "text"}]}}),
                                    wake_line("cmsg_A", "later wake is not the session's start")]),
        ]
        self.run_apply(files)
        self.assertEqual(self.topic("custom"), ("Mine", "custom"))
        self.assertEqual(self.topic("ai"), ("Thread title", "thread"))
        self.assertEqual(self.topic("ai2"), ("AI guess", "ai"))       # prompt never beats ai
        self.assertEqual(self.topic("bare"), ("first & only", "prompt"))
        self.assertEqual(self.topic("plain"), (None, None))
        self.assertEqual(self.topic("blocks"), (None, None))

    def test_prompt_upgrades_later_and_eviction_keeps_title(self):
        f = [self.session("s", [wake_line("cmsg_A", "the ask")])]
        self.run_apply(f)
        self.assertEqual(self.topic("s"), ("the ask", "prompt"))
        self.cache("a", coordinator(spawn("t", "Real title", message_id="cmsg_A")))
        self.run_apply(f)
        self.assertEqual(self.topic("s"), ("Real title", "thread"))
        (self.blobs / "a").unlink()                                 # cache evicted
        self.run_apply(f)
        self.assertEqual(self.topic("s"), ("Real title", "thread"))

    def test_missing_or_junk_cache_is_quiet(self):
        (self.blobs / "junk").write_bytes(b"\xff\x11\x02\x99garbage")
        truncated = b"\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0f" + v8({"k": "value"})[:-4]
        (self.blobs / "trunc").write_bytes(b"\xff\x11\x02" + snappy_literal(truncated))
        f = [self.session("s", [wake_line("cmsg_A", "ask")])]
        self.assertEqual(pt.apply_thread_titles(self.conn, f, self.tmp / "nope"), 1)
        self.assertEqual(self.topic("s"), ("ask", "prompt"))
        self.assertEqual(pt.load_thread_titles(self.tmp / "blob"), {})

    def test_self_referencing_value_terminates(self):
        raw = b"o" + v8("self") + b"^" + _varint(0) + b"{" + _varint(1)  # obj.self = obj
        inner = b"\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0f" + raw
        (self.blobs / "cyc").write_bytes(b"\xff\x11\x02" + snappy_literal(inner))
        v = pt.decode_blob((self.blobs / "cyc").read_bytes())
        self.assertIs(v["self"], v)
        self.assertEqual(pt.load_thread_titles(self.tmp / "blob"), {})

    def test_uncompressed_blob(self):
        root = coordinator(spawn("t", "Plain", message_id="cmsg_A"))
        (self.blobs / "plain").write_bytes(b"\xff\x15\xfe" + b"\x00" * 12 + b"\xff\x0f" + v8(root))
        self.assertEqual(pt.load_thread_titles(self.tmp / "blob").get("cmsg_A"), "Plain")

    def test_malformed_record_does_not_abort(self):
        f = [self.session("s", [json.dumps({"type": "user", "message": "malformed"}), json.dumps([1]),
                                wake_line("cmsg_A", "ask")])]
        self.assertEqual(self.run_apply(f), 1)
        self.assertEqual(self.topic("s"), ("ask", "prompt"))

    def test_shared_instructions_fall_back_to_ids(self):
        self.cache("a", coordinator(spawn("t1", "Title A", message_id="cmsg_A", instructions="Same."),
                                    spawn("t2", "Title B", message_id="cmsg_B", instructions="Same.")))
        self.assertNotIn(pt.note_key("Same."), pt.load_thread_titles(self.tmp / "blob"))
        f = [self.session("b", [relay_line("Same."), wake_line("cmsg_B", "x")])]
        self.run_apply(f)
        self.assertEqual(self.topic("b"), ("Title B", "thread"))

    def test_rename_after_spawn_wins(self):
        rename = [{"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r", "name": "mcp__hearthbot__rename_thread",
             "input": {"thread_id": "cmsg_A", "title": "New"}}]}}]
        self.cache("a", coordinator(spawn("t", "Old", message_id="cmsg_A"), rename))
        self.assertEqual(pt.load_thread_titles(self.tmp / "blob")["cmsg_A"], "New")
        # same order when the two events are sibling values of one object
        self.cache("a", {"x": spawn("t", "Old", message_id="cmsg_A")[0], "y": rename[0]})
        self.assertEqual(pt.load_thread_titles(self.tmp / "blob")["cmsg_A"], "New")


    def test_rename_refreshes_captured_title(self):
        f = [self.session("s", [wake_line("cmsg_A", "x")])]
        self.cache("a", coordinator(spawn("t", "Old", message_id="cmsg_A")))
        self.run_apply(f)
        self.assertEqual(self.topic("s"), ("Old", "thread"))
        self.cache("a", coordinator(spawn("t", "New", message_id="cmsg_A")))
        self.assertEqual(self.run_apply(f), 1)
        self.assertEqual(self.topic("s"), ("New", "thread"))
        self.assertEqual(self.run_apply(f), 0)                      # unchanged: no write

    def test_note_follows_rename(self):
        rename = [{"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r", "name": "mcp__hearthbot__rename_thread",
             "input": {"thread_id": "cmsg_A", "title": "New"}}]}}]
        self.cache("a", coordinator(spawn("t", "Old", message_id="cmsg_A", instructions="Go."), rename))
        f = [self.session("n", [relay_line("Go.")]),                 # note is its only key
             self.session("i", [relay_line("Go."), wake_line("cmsg_A", "x")])]
        self.run_apply(f)
        self.assertEqual(self.topic("n"), ("New", "thread"))
        self.assertEqual(self.topic("i"), ("New", "thread"))

    def test_ids_beat_note(self):
        self.cache("a", coordinator(spawn("t1", "By note", message_id="cmsg_X", instructions="Go."),
                                    spawn("t2", "By id", message_id="cmsg_B")))
        f = [self.session("b", [relay_line("Go."), wake_line("cmsg_B", "x")])]
        self.run_apply(f)
        self.assertEqual(self.topic("b"), ("By id", "thread"))

    def test_wake_uses_trigger_message(self):
        line = json.dumps({"type": "user", "cwd": "C:\\repo", "message": {"role": "user", "content":
            '<wake reason="mention"><project id="chan_1" type="project">'
            '<message id="cmsg_OLD">earlier</message>'
            '<message trigger="true" id="cmsg_A">the real ask</message>'
            '<message id="cmsg_LATER">after</message></project></wake>'}})
        self.cache("a", coordinator(spawn("t1", "Wrong", message_id="cmsg_OLD"),
                                    spawn("t2", "Right", message_id="cmsg_A")))
        f = [self.session("s", [line])]
        self.assertEqual(pt.thread_keys(f[0])[1], "the real ask")
        self.run_apply(f)
        self.assertEqual(self.topic("s"), ("Right", "thread"))


class AiTitleBeatsPromptTest(unittest.TestCase):
    """End to end through the real scan: a later ai-title replaces the prompt fallback."""

    def test_ai_title_replaces_prompt(self):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "projects" / "p"
        proj.mkdir(parents=True)
        db = tmp / "u.db"
        f = proj / "s1.jsonl"
        base = {"sessionId": "s1", "cwd": "C:\\repo", "timestamp": "2026-10-01T00:00:00Z"}
        wake = dict(json.loads(wake_line("cmsg_A", "the ask")), **base)
        turn = dict(base, type="assistant", message={"id": "msg_1", "role": "assistant",
                    "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}})
        f.write_text(json.dumps(wake) + "\n" + json.dumps(turn) + "\n", encoding="utf-8")

        def scan():
            try:
                import sources
                sources.scan_all(db_path=db, verbose=False,
                                 sources=[sources.ClaudeSource(projects_dirs=[str(tmp / "projects")])])
            except ImportError:
                scanner.scan(db_path=db, projects_dirs=[tmp / "projects"], verbose=False)

        def topic():
            c = sqlite3.connect(db)
            try:
                return c.execute("SELECT topic, topic_source FROM sessions WHERE session_id='s1'").fetchone()
            finally:
                c.close()

        mtime = f.stat().st_mtime
        scan()
        self.assertEqual(topic(), ("the ask", "prompt"))
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ai-title", "sessionId": "s1", "aiTitle": "AI title"}) + "\n")
        # Same timestamp tick as the first scan (what a fast machine produces): the append
        # leaves mtime unchanged, and the scan must still pick the new line up.
        os.utime(f, (mtime, mtime))
        scan()
        self.assertEqual(topic(), ("AI title", "ai"))


if __name__ == "__main__":
    unittest.main()
