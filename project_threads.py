"""Titles for Claude Projects thread sessions.

A Projects thread runs as an ordinary local Claude Code session, but its title is
never written to the transcript: the cloud coordinator names the thread when it
spawns it (``mcp__hearthbot__start_rc_session`` / ``start_thread_session`` /
``start_folder_thread``, input ``title``) and only claude.ai stores it. The
Claude desktop app caches each coordinator conversation's events in its claude.ai
IndexedDB, as large values spilled to blob files: Blink wrapper + snappy + V8
ValueSerializer. This module decodes those blobs (stdlib only), maps each spawned
thread's trigger message id (``cmsg_...``) and session id (``cse_...``) to its
title, and fills ``sessions.topic`` for the matching local transcripts.

The local join keys come from the thread's first user record: its ``<wake>``
envelope names the trigger ``<message id="cmsg_...">`` and the worktree cwd may
carry ``bridge-cse_<id>``. When the cache has evicted a thread's spawn (it is a
rolling window), the trigger message body is used instead (topic_source
'prompt'), so a Projects thread is never left Untitled.

Precedence (higher wins, equal may refresh): custom > thread > ai > prompt.
Read-only against the cache; a missing or undecodable blob is skipped quietly.
"""

import html
import json
import os
import re
import struct
import sys
from pathlib import Path


def default_blob_dir():
    """The desktop app's claude.ai IndexedDB blob dir for this OS (may not exist)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Claude" / "IndexedDB" / "https_claude.ai_0.indexeddb.blob"


# --- snappy (raw block format) ---------------------------------------------

def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def unsnappy(b):
    n, i = _varint(b, 0)
    out = bytearray()
    while i < len(b):
        t = b[i]
        i += 1
        kind = t & 3
        if kind == 0:  # literal
            ln = t >> 2
            if ln >= 60:
                nb = ln - 59
                ln = int.from_bytes(b[i:i + nb], "little")
                i += nb
            ln += 1
            out += b[i:i + ln]
            i += ln
            continue
        if kind == 1:
            ln = ((t >> 2) & 7) + 4
            off = ((t >> 5) << 8) | b[i]
            i += 1
        elif kind == 2:
            ln = (t >> 2) + 1
            off = int.from_bytes(b[i:i + 2], "little")
            i += 2
        else:
            ln = (t >> 2) + 1
            off = int.from_bytes(b[i:i + 4], "little")
            i += 4
        if off == 0 or off > len(out):
            raise ValueError("bad snappy copy offset")
        start = len(out) - off
        if off >= ln:
            out += out[start:start + ln]
        else:  # overlapping copy: repeats the last `off` bytes
            for k in range(ln):
                out.append(out[start + k])
    if len(out) != n:
        raise ValueError("snappy length mismatch")
    return bytes(out)


# --- V8 ValueSerializer (the subset IndexedDB JSON-ish values use) ----------

class _V8:
    def __init__(self, b, i):
        self.b, self.i, self.ids = b, i, []

    def varint(self):
        v, self.i = _varint(self.b, self.i)
        return v

    def raw(self, n):
        s = self.b[self.i:self.i + n]
        self.i += n
        return s

    def value(self):
        b = self.b
        while b[self.i] == 0:  # padding
            self.i += 1
        t = chr(b[self.i])
        self.i += 1
        if t == "_":
            return None
        if t == "0":
            return None
        if t == "T":
            return True
        if t == "F":
            return False
        if t == "I":
            v = self.varint()
            return (v >> 1) ^ -(v & 1)
        if t == "U":
            return self.varint()
        if t == "N":
            return struct.unpack("<d", self.raw(8))[0]
        if t == "D":
            v = struct.unpack("<d", self.raw(8))[0]
            self.ids.append(v)
            return v
        if t == '"':
            return self.raw(self.varint()).decode("latin-1")
        if t == "c":
            return self.raw(self.varint()).decode("utf-16-le", "replace")
        if t == "S":
            return self.raw(self.varint()).decode("utf-8", "replace")
        if t == "^":
            return self.ids[self.varint()]
        if t == "o":
            obj = {}
            self.ids.append(obj)
            while True:
                while b[self.i] == 0:
                    self.i += 1
                if b[self.i] == ord("{"):
                    self.i += 1
                    self.varint()
                    return obj
                k = self.value()
                obj[str(k)] = self.value()
        if t == "A":
            arr = []
            self.ids.append(arr)
            for _ in range(self.varint()):
                while b[self.i] == 0:
                    self.i += 1
                if b[self.i] == ord("-"):  # hole
                    self.i += 1
                    arr.append(None)
                else:
                    arr.append(self.value())
            self._props_until("$")
            return arr
        if t == "a":
            arr = [None] * self.varint()
            self.ids.append(arr)
            for k, v in self._props_until("@"):
                if isinstance(k, int) and 0 <= k < len(arr):
                    arr[k] = v
            return arr
        raise ValueError("unsupported V8 tag %r at %d" % (t, self.i - 1))

    def _props_until(self, end):
        props = []
        while True:
            while self.b[self.i] == 0:
                self.i += 1
            if self.b[self.i] == ord(end):
                self.i += 1
                self.varint()
                self.varint()
                return props
            k = self.value()
            props.append((k, self.value()))


def decode_blob(data):
    """Decode one IndexedDB blob file's bytes to a Python value (raises on junk)."""
    if len(data) < 4 or data[0] != 0xFF:
        raise ValueError("not a Blink-wrapped value")
    if data[1] == 0x11:              # IDB wrapper pseudo-version (idb_value_wrapping.cc)
        if data[2] != 2:             # 0x01 = replaced-with-blob reference, no value here
            raise ValueError("not a snappy-wrapped value")
        data = unsnappy(data[3:])
    i = 0
    seen = 0
    while i < len(data) and data[i] == 0xFF:  # Blink envelope, then V8 header
        _, i = _varint(data, i + 1)
        seen += 1
        if i < len(data) and data[i] == 0xFE:  # trailer offset tag: u64 offset + u32 size
            i += 13
    if not seen:
        raise ValueError("missing V8 header")
    return _V8(data, i).value()


# --- thread titles ----------------------------------------------------------

SPAWN_TOOLS = ("start_rc_session", "start_thread_session", "start_folder_thread",
               "rename_thread")


def _walk(v):
    """Dicts in document order (so a later rename beats its spawn). V8 object
    references can make the graph cyclic, so each container is visited once."""
    stack, seen = [v], set()
    while stack:
        x = stack.pop()
        if not isinstance(x, (dict, list)) or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, dict):
            yield x
            stack.extend(reversed(list(x.values())))
        else:
            stack.extend(reversed(x))


_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(#(cmsg_\w+)\)")
_NOTE = re.compile(r'<relay\b[^>]*\breason="spawn"[^>]*>.*?<note>(.*?)</note>', re.S)


def note_key(text):
    """Join key for a spawn's ``instructions``: the thread's first relay note carries
    the same text (html-escaped, re-indented), so compare it normalised."""
    return "note:" + " ".join(html.unescape(text).split())


def titles_from_value(root):
    """({id: title} from spawn calls, {note: {titles}}, {cmsg: title} from thread
    links) for one decoded coordinator conversation. Spawn ids are the thread's
    cmsg_ id and its cse_ session id; the note: key of the spawn instructions is
    kept as a set so two spawns sharing instructions can be told apart (ambiguous
    notes are dropped). The coordinator also links threads as
    ``[Title](#cmsg_...)`` in its posts, which survives in the cache after the
    spawn itself has scrolled out."""
    calls, results, links = {}, {}, {}
    for d in _walk(root):
        if d.get("type") == "tool_use" and str(d.get("name", "")).split("__")[-1] in SPAWN_TOOLS:
            inp = d.get("input") or {}
            if isinstance(inp, dict) and isinstance(inp.get("title"), str) and inp["title"].strip():
                calls[d.get("id")] = inp
        elif d.get("type") == "tool_result" and d.get("tool_use_id"):
            results[d["tool_use_id"]] = d.get("content")
        for v in d.values():
            if isinstance(v, str) and "](#cmsg_" in v:
                for title, cmsg in _LINK.findall(v):
                    links[cmsg] = title.strip()
    spawns, notes = {}, {}
    for tid, inp in calls.items():
        title = inp["title"].strip()
        keys = {inp.get("message_id"), inp.get("thread_id")}
        if isinstance(inp.get("instructions"), str):
            # Remember which thread the note belongs to, not just the title, so a
            # later rename of that thread also renames what the note resolves to.
            cid = next((k for k in keys if isinstance(k, str) and k.startswith("cmsg_")), None)
            notes.setdefault(note_key(inp["instructions"]), set()).add((cid, title))
        res = results.get(tid)
        for blk in res if isinstance(res, list) else [res]:
            text = blk.get("text") if isinstance(blk, dict) else blk
            try:
                j = json.loads(text) if isinstance(text, str) else None
            except ValueError:
                j = None
            if isinstance(j, dict):
                keys.update((j.get("thread_id"), j.get("session_id")))
        for k in keys:
            if isinstance(k, str) and k.startswith(("cmsg_", "cse_")):
                spawns[k] = title
    return spawns, notes, links


def load_thread_titles(blob_dir):
    """Scan every blob under ``blob_dir``; skip anything that isn't decodable.
    A spawn's own title beats a link's anchor text for the same key."""
    spawns, notes, links = {}, {}, {}
    root = Path(blob_dir)
    if not root.is_dir():
        return {}
    blobs = []
    for p in root.rglob("*"):
        try:
            if p.is_file():
                blobs.append((p.stat().st_mtime, p))
        except OSError:
            continue
    for _, p in sorted(blobs):  # oldest first, so a later rename/respawn wins
        try:
            s, n, l = titles_from_value(decode_blob(p.read_bytes()))
        except Exception:  # cache files are private + rewritten under us; never fatal
            continue
        spawns.update(s)
        links.update(l)
        for k, ts in n.items():
            notes.setdefault(k, set()).update(ts)
    unique = {}
    for k, pairs in notes.items():
        resolved = {spawns.get(cid, t) if cid else t for cid, t in pairs}
        if len(resolved) == 1:  # a note shared by two threads identifies neither
            unique[k] = resolved.pop()
    return {**links, **unique, **spawns}


_WAKE_MSG = re.compile(r'<message\b([^>]*)>(.*?)</message>', re.S)
_MSG_ID = re.compile(r'\bid="(cmsg_\w+)"')
_BRIDGE_CSE = re.compile(r"bridge-(cse_[A-Za-z0-9]+)")
_TAGS = re.compile(r"<[^>]+>")


def thread_keys(jsonl_path, max_lines=200):
    """(join keys, prompt_title) for a Projects thread transcript, or None when the
    session isn't one. Reads only the head: the coordinator's spawn relay (a meta
    user record or queued prompt) and the first real user record, a ``<wake>``.
    Keys, best first: trigger cmsg_, bridge cse_ from the cwd, spawn note (text,
    so the weakest: two spawns can share it)."""
    keys, prompt, note = [], None, None
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n >= max_lines or ("<relay" not in line and "<wake" not in line
                                      and '"user"' not in line):
                    if n >= max_lines:
                        break
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("type") == "queue-operation":
                    c = r.get("content")
                elif r.get("type") == "user":
                    msg = r.get("message")
                    if not isinstance(msg, dict):
                        continue  # malformed, not a prompt
                    c = msg.get("content")
                else:
                    continue
                if not isinstance(c, str):
                    if r.get("type") == "user" and not r.get("isMeta"):
                        break  # first real prompt isn't a wake: not a thread
                    continue
                m = _NOTE.search(c)
                if m and note is None:
                    note = note_key(m.group(1))
                if r.get("type") != "user" or r.get("isMeta"):
                    continue
                msgs = [x for x in _WAKE_MSG.findall(c) if _MSG_ID.search(x[0])] if "<wake" in c else []
                # A wake can replay earlier messages; the one it is about is the trigger.
                trig = next((x for x in msgs if 'trigger="true"' in x[0]), msgs[-1] if msgs else None)
                if trig:
                    keys.append(_MSG_ID.search(trig[0]).group(1))
                    body = " ".join(html.unescape(_TAGS.sub(" ", trig[1])).split())
                    prompt = (body[:77] + "...") if len(body) > 80 else body or None
                cse = _BRIDGE_CSE.search(str(r.get("cwd") or "") + " " + str(r.get("gitBranch") or ""))
                if cse:
                    keys.append(cse.group(1))
                break
    except OSError:
        return None
    if note:
        keys.append(note)
    return (keys, prompt) if keys else None


_RANK = {"prompt": 1, "ai": 2, "thread": 3, "custom": 4}


def apply_thread_titles(conn, jsonl_files, blob_dir):
    """Fill sessions.topic for Projects thread sessions. Returns rows updated.

    A session is touched when its current topic ranks below the new one, or when
    it already has a thread title and the thread was renamed. A 'prompt' fallback
    never replaces an ai-title, and nothing beats a custom title. A title that has
    dropped out of the cache keeps whatever was captured earlier (no update
    without a source)."""
    rows = conn.execute(
        "SELECT session_id, topic, topic_source FROM sessions "
        "WHERE COALESCE(topic_source, '') <> 'custom' "
        "   OR topic IS NULL OR topic = ''").fetchall()
    want = {r[0]: (_RANK.get(r[2] or "", 0) if (r[1] or "") else 0, r[1]) for r in rows}
    if not want:
        return 0
    titles = None
    updated = 0
    for path in jsonl_files:
        sid = Path(path).stem
        if sid not in want:
            continue
        try:
            keys = thread_keys(path)
        except Exception:  # one odd transcript must never abort the scan
            keys = None
        if not keys:
            continue
        keys, prompt = keys
        if titles is None:
            titles = load_thread_titles(blob_dir) if blob_dir else {}
        title = next((titles[k] for k in keys if k in titles), None)
        source = "thread" if title else "prompt"
        title = title or prompt
        rank, current = want[sid]
        renamed = source == "thread" and rank == _RANK["thread"] and title != current
        if not title or (_RANK[source] <= rank and not renamed):
            continue
        conn.execute("UPDATE sessions SET topic = ?, topic_source = ? WHERE session_id = ?",
                     (title, source, sid))
        want[sid] = (_RANK[source], title)
        updated += 1
    if updated:
        conn.commit()
    return updated
