"""Worktree sessions fold into their main repo's project (idea from upstream claude-usage)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import scanner
from scanner import get_db, init_db, project_name_from_cwd


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestWorktreeProjectName(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.main = self.tmp / "work" / "myrepo"
        self.main.mkdir(parents=True)
        _git("init", "-q", cwd=self.main)
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init", cwd=self.main)
        self.wt = self.tmp / "work" / "myrepo-lane2"
        _git("worktree", "add", "-q", str(self.wt), cwd=self.main)
        (self.main / "sub").mkdir()
        scanner.worktree_main_root.cache_clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_linked_worktree_folds_into_main_repo(self):
        self.assertEqual(project_name_from_cwd(str(self.wt)), "work/myrepo")

    def test_main_checkout_and_its_subfolders_keep_path_names(self):
        # Narrower than upstream on purpose: only worktrees move, so no
        # historical project name changes for ordinary repos.
        self.assertEqual(project_name_from_cwd(str(self.main)), "work/myrepo")
        self.assertEqual(project_name_from_cwd(str(self.main / "sub")), "myrepo/sub")

    def test_claude_worktree_path_folds_even_when_deleted(self):
        self.assertEqual(
            project_name_from_cwd(r"C:\gone\proj\.claude\worktrees\agent-abc"), "gone/proj")

    def test_backfill_renames_only_worktree_sessions(self):
        db = self.tmp / "u.db"
        conn = get_db(db)
        init_db(conn)
        for sid, cwd, name in (("s-wt", str(self.wt), "work/myrepo-lane2"),
                               ("s-sub", str(self.main / "sub"), "myrepo/sub")):
            conn.execute("INSERT INTO sessions (session_id, project_name) VALUES (?, ?)", (sid, name))
            conn.execute("""INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
                VALUES (?, '2026-09-22T10:00:00Z', 'claude-opus-5-5', 1, 1, 0, 0, NULL, ?, ?)""",
                         (sid, cwd, "m-" + sid))
        conn.commit()
        self.assertEqual(scanner.backfill_worktree_projects_once(conn, verbose=False), 1)
        names = dict(conn.execute("SELECT session_id, project_name FROM sessions").fetchall())
        self.assertEqual(names, {"s-wt": "work/myrepo", "s-sub": "myrepo/sub"})
        self.assertEqual(scanner.backfill_worktree_projects_once(conn, verbose=False), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
