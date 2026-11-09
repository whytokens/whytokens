"""
config.py - whytokens-wide defaults (DB location + dashboard port).

Kept separate from upstream claude-usage so the two can run side by side:
claude-usage defaults to ``~/.claude/usage.db`` on port 8080, while whytokens uses
its own database and port below. Both read the *same* Claude Code transcripts
(``~/.claude/projects``).

This is the single source for these two values; ``cli.py`` and ``api.py`` import
them, and ``cmd_scan`` passes ``DB_PATH`` explicitly into the (vendored) scanner so
it writes here rather than to the scanner's default path. Override the port at runtime
with the ``PORT`` environment variable.
"""

import os
import sys
import tempfile
from pathlib import Path

import project_threads

# Own data dir (WHYTOKENS_HOME), DB file overridable with WHYTOKENS_DB.
# Under unittest the default home is a throwaway dir, so no test run can read the
# user's config.json (and its extensions) or write the real database
# (unittest discovery may import this before any tests/__init__ bootstrap runs).
_TESTING = "unittest" in sys.modules and not os.environ.get("WHYTOKENS_HOME")
DATA_DIR = Path(os.environ.get("WHYTOKENS_HOME") or
                (tempfile.mkdtemp(prefix="whytokens-test-") if _TESTING else Path.home() / ".whytokens"))
DB_PATH = Path(os.environ.get("WHYTOKENS_DB") or (DATA_DIR / "whytokens.db"))

# Default dashboard port (claude-usage uses 8080). Override with env PORT.
DEFAULT_PORT = 8090

# OpenAI Codex CLI home (second ingestion source). Codex stores
# per-session rollout transcripts under <CODEX_HOME>/sessions/YYYY/MM/DD/. Honor
# the CODEX_HOME env var Codex itself uses; default to ~/.codex.
CODEX_PATH = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

# Claude desktop app's claude.ai IndexedDB blob cache: the only local copy of
# Projects thread titles (see project_threads.py).
DESKTOP_BLOB_DIR = project_threads.default_blob_dir()


def desktop_cache_dir():
    """The desktop-app cache dir, or None unless the user opted in.

    Reading another app's private cache is opt-in: set
    WHYTOKENS_DESKTOP_CACHE=1. Scope when on: only ``DESKTOP_BLOB_DIR``, read-only.
    """
    on = os.environ.get("WHYTOKENS_DESKTOP_CACHE", "").strip().lower() in ("1", "true", "yes", "on")
    return DESKTOP_BLOB_DIR if on else None


def claude_projects_dirs(base):
    """Return ``base`` Claude transcript roots plus any extras named by the
    ``WHYTOKENS_EXTRA_PROJECTS_DIRS`` env var (a ';'- or ','-separated list),
    de-duplicated and order-preserving.

    Lets a containerized/sandboxed eval run point the profiler at bind-mounted
    transcripts alongside the host's ``~/.claude/projects``. Env unset → ``base``
    unchanged. Read at scan time (not import time) so a run can set it late, and so
    the vendored ``scanner.py`` stays free of profiler-specific path policy — only
    the default-path scan sites (``sources/claude.py``, ``cli.py doctor``) consult
    this, exactly matching the old behavior of mutating ``DEFAULT_PROJECTS_DIRS``.
    """
    dirs = [Path(d) for d in base]
    raw = os.environ.get("WHYTOKENS_EXTRA_PROJECTS_DIRS") or ""
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            p = Path(part)
            if p not in dirs:
                dirs.append(p)
    return dirs


def discovered_roots(projects_dir=None):
    """The one discovery rule: every transcript root a scan reads, as
    ``[{source, path, exists}]``. ``sources``, the doctor and ``/api/status`` all
    resolve roots through this (Claude via ``claude_projects_dirs``, Codex under
    ``CODEX_PATH``). ``projects_dir`` overrides only the Claude roots."""
    import scanner  # local: scanner imports nothing from config, but keep config import-light
    claude = [Path(projects_dir)] if projects_dir else claude_projects_dirs(scanner.DEFAULT_PROJECTS_DIRS)
    roots = [{"source": "claude", "path": str(d), "exists": Path(d).is_dir()} for d in claude]
    codex = CODEX_PATH / "sessions"
    roots.append({"source": "codex", "path": str(codex), "exists": codex.is_dir()})
    return roots
