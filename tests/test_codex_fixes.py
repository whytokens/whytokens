"""Codex source edge cases: partial last line, cumulative reset, zstd lookup, doctor payload drift."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import schema
from sources import codex
from sources.codex import parse_file


def _tok(ts, i, c, o, r=0):
    return {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": i, "cached_input_tokens": c,
                                       "output_tokens": o, "reasoning_output_tokens": r,
                                       "total_tokens": i + o}}}}


def _write(lines, trailing_newline=True):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8", newline="\n")
    text = "\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines)
    f.write(text + ("\n" if trailing_newline else ""))
    f.close()
    return f.name


class TestPartialLastLine(unittest.TestCase):
    def test_incomplete_final_line_is_not_counted(self):
        full = json.dumps(_tok("2026-01-01T00:00:02Z", 200, 0, 20))
        path = _write([_tok("2026-01-01T00:00:01Z", 100, 0, 10), full[:30]], trailing_newline=False)
        _, turns, n = parse_file(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(n, 1)  # the cut line is not a line yet: growth must re-trigger a parse

    def test_complete_final_line_without_newline_is_counted(self):
        path = _write([_tok("2026-01-01T00:00:01Z", 100, 0, 10)], trailing_newline=False)
        _, turns, n = parse_file(path)
        self.assertEqual((len(turns), n), (1, 1))

    def test_missing_reasoning_subtotal_is_not_a_reset(self):
        a = _tok("2026-01-01T00:00:01Z", 100, 0, 100, r=40)
        b = _tok("2026-01-01T00:00:02Z", 150, 0, 150)
        del b["payload"]["info"]["total_token_usage"]["reasoning_output_tokens"]
        t = parse_file(_write([a, b]))[1][1]
        self.assertEqual(t["output_tokens"], 50)


class TestCumulativeReset(unittest.TestCase):
    def test_reset_uses_whole_tuple(self):
        # total input falls while cached and output rise: a reset, so the new totals are the delta
        path = _write([_tok("2026-01-01T00:00:01Z", 5000, 100, 100),
                       _tok("2026-01-01T00:00:02Z", 3000, 2500, 200)])
        _, turns, _ = parse_file(path)
        t = turns[1]
        self.assertEqual((t["cache_read_tokens"], t["output_tokens"], t["input_tokens"]), (2500, 200, 500))


    def test_reset_detected_when_only_output_falls(self):
        path = _write([_tok("2026-01-01T00:00:01Z", 5000, 100, 900),
                       _tok("2026-01-01T00:00:02Z", 6000, 200, 50)])
        t = parse_file(path)[1][1]
        self.assertEqual((t["output_tokens"], t["cache_read_tokens"]), (50, 200))


class TestZstdLookup(unittest.TestCase):
    def test_cwd_copy_is_never_run(self):
        d = tempfile.mkdtemp()
        exe = Path(d) / ("zstd.exe" if os.name == "nt" else "zstd")
        exe.write_text("x")
        os.chmod(exe, 0o755)
        old = os.getcwd()
        os.chdir(d)

        def which311(cmd, path=None):  # Python 3.11 on Windows: the cwd is searched first
            for p in [os.curdir] + (path or "").split(os.pathsep):
                for ext in ("", ".exe"):
                    f = os.path.join(p, cmd + ext)
                    if os.path.isfile(f):
                        return f
        try:
            with mock.patch.object(codex.shutil, "which", which311), \
                 mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "rel"])}):
                self.assertFalse(codex._zstd_exe())
            with mock.patch.object(codex.shutil, "which", which311), \
                 mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(os.path.normcase(codex._zstd_exe()), os.path.normcase(str(exe)))
        finally:
            os.chdir(old)


class TestDoctorPayloadShape(unittest.TestCase):
    def test_non_object_payload_is_drift(self):
        info = schema.inspect_codex_record({"type": "event_msg", "payload": [1, 2]})
        self.assertTrue(any(d.startswith("payload:") for d in info["shape_drift"]))


if __name__ == "__main__":
    unittest.main()
