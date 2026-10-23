"""Guard the guard: break each data-policy / server-hardening rule in a temp copy of the
repo and confirm tests/test_data_policy.py + tests/test_server_hardening.py go red.

    python scripts/falsify_data_policy.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original, mutant) - each must turn the suites red on its own.
MUTANTS = [
    # allowlist at every writer
    ("scanner.py", "    return policy.redact_calls(calls)", "    return calls"),
    ("sources/codex.py", "json.dumps(policy.redact_calls(calls))", "json.dumps(calls)"),
    ("policy.py", "for key in _KEEP.get(name, ()):", "for key in inp:"),
    ("policy.py", "    if store_raw_inputs():\n        return calls\n", "    if True:\n        return calls\n"),
    ("policy.py", "    if name in _COMMAND_TOOLS:", "    if False:"),
    ("policy.py", "def store_raw_inputs():\n    return ", "def store_raw_inputs():\n    return True or "),
    ("policy.py", "    out = {}\n    size", "    out = dict(inp)\n    size"),
    # migration
    ("sources/__init__.py", "    policy.migrate_tool_calls(conn, scanner._meta_get, scanner._meta_set)\n", ""),
    ("policy.py", '        conn.execute("VACUUM")', "        pass"),
    ("policy.py", "    if store_raw_inputs() or meta_get(conn, MIGRATION_KEY):", "    if meta_get(conn, MIGRATION_KEY):"),
    ("policy.py", "    meta_set(conn, MIGRATION_KEY, \"1\")\n", ""),
    ("policy.py", "    if not busy:\n", "    if True:\n"),
    ("policy.py", "    except sqlite3.OperationalError:\n        return len(updates)\n",
     "    except sqlite3.OperationalError:\n        meta_set(conn, MIGRATION_KEY, \"1\")\n        return len(updates)\n"),
    ("policy.py", "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA wal_checkpoint(PASSIVE)"),
    ("scanner.py", "    policy.migrate_tool_calls(conn, _meta_get, _meta_set)\n", ""),
    # waste prose
    ("waste.py", r're.split(r"[\\/]", str(path))[-1]', "str(path)"),
    # desktop cache opt-in
    ("config.py", "return DESKTOP_BLOB_DIR if on else None", "return DESKTOP_BLOB_DIR"),
    ("sources/coordinator.py", "        if root is None:\n            return []\n", ""),
    # egress boundary (the test must notice a socket or a subprocess in scan)
    ("sources/__init__.py", "    scanner.backfill_worktree_projects_once(conn, verbose=verbose)\n",
     "    import socket; socket.create_connection(('127.0.0.1', 9))\n    scanner.backfill_worktree_projects_once(conn, verbose=verbose)\n"),
    ("sources/__init__.py", "    scanner.backfill_worktree_projects_once(conn, verbose=verbose)\n",
     "    import subprocess; subprocess.Popen(['x'])\n    scanner.backfill_worktree_projects_once(conn, verbose=verbose)\n"),
    # server
    ("api.py", "if not self.allow_any_host and _host_name(host) not in LOOPBACK_HOSTS:", "if False:"),
    ("api.py", "if origin is not None and origin != f\"http://{host}\":", "if False:"),
    ("api.py", 'if self.headers.get("Sec-Fetch-Site") == "cross-site":', "if False:"),
    ("api.py", "if not self.token or not secrets.compare_digest(sent, self.token):", "if False:"),
    ("api.py", "if not self.token or not secrets.compare_digest(sent, self.token):", "if self.token and not secrets.compare_digest(sent, self.token):"),
    ("api.py", "        if not self._guard(post=True):\n            return\n", ""),
    ("api.py", "        if not self._guard():\n            return\n", ""),
    ("api.py", "if _host_name(host) not in LOOPBACK_HOSTS and not unsafe_host:", "if False:"),
    ("api.py", "    DashboardHandler.allow_any_host = unsafe_host\n", ""),
    ("api.py", "    DashboardHandler.token = secrets.token_urlsafe(32)\n", ""),
    ("api.py", "meta + b\"</head>\"", "b\"</head>\""),
    ("static/modules/api.js", "'X-Whytokens-Token':", "'X-Unused':"),
]
SUITES = ["tests.test_data_policy", "tests.test_server_hardening"]


HUNG = "HUNG"


def run(tree, timeout=60):
    """Exit code, or HUNG if the suites outlive ``timeout`` (process tree killed)."""
    p = subprocess.Popen([sys.executable, "-m", "unittest", *SUITES], cwd=tree,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)] if sys.platform == "win32"
                       else ["kill", "-9", str(p.pid)], capture_output=True)
        p.wait()
        return HUNG


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db"))
        base = run(tree)
        if base != 0:
            sys.exit(f"baseline suites are {'HUNG' if base == HUNG else 'red'}; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig!r}")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            rc = run(tree)
            path.write_text(src, encoding="utf-8")
            verdict = "HUNG" if rc == HUNG else "caught" if rc != 0 else "MISSED"
            print(f"{verdict} {fname}: {orig.strip()[:60]}", flush=True)
            failures += verdict != "caught"
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
