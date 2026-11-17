"""Verdicts of fixes.verify on the standard before/after fixtures, as plain JSON.
The golden file tests/fixtures/verify_golden.json was captured before shapes existed;
tests/test_shape.py requires the same output today."""
import json
import shutil
import sqlite3

import fixes
from scanner import init_db
from tests.test_fixes import FIX_DATE, _build

CASES = {
    "unchanged_wasteful": ([True] * 12, [True] * 12, "resident_output"),
    "clean_unchanged": ([False] * 12, [False] * 12, "resident_output"),
    "real_drop": ([True] * 12, [False] * 12, "resident_output"),
    "other_cause": ([True] * 12, [False] * 12, "idle_gap"),
    "increase": ([False] * 12, [True] * 12, "resident_output"),
    "short_before": ([True] * 9, [False] * 12, "resident_output"),
    "short_after": ([True] * 12, [False] * 9, "resident_output"),
}


def capture():
    out = {}
    for name, (before, after, cause) in CASES.items():
        tmp, db = _build(before, after)
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            init_db(conn)
            fixes.record_fix(conn, "acme/app", cause, "trim bash output", FIX_DATE)
            fix = dict(fixes.list_fixes(conn)[0])
            conn.close()
            fix.pop("recorded_at", None)
            r = fixes.verify(db, fix)
            out[name] = json.loads(json.dumps(r, sort_keys=True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    import sys
    json.dump(capture(), sys.stdout, indent=1, sort_keys=True)
