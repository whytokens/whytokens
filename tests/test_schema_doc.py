"""docs/SCHEMA.md documents every column of every core table (the schema is the contract)."""
import re
import sqlite3
import unittest
from pathlib import Path

import scanner

DOC = Path(__file__).resolve().parent.parent / "docs" / "SCHEMA.md"


class SchemaDocTest(unittest.TestCase):

    def test_every_core_column_is_documented(self):
        doc = DOC.read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        scanner.init_db(conn)
        missing = []
        for table in scanner.CORE_TABLES:
            if not re.search(r"### `%s`" % table, doc):
                missing.append(table)
            missing += [f"{table}.{c[1]}" for c in conn.execute(f"PRAGMA table_info({table})")
                        if f"`{c[1]}`" not in doc]
        conn.close()
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
