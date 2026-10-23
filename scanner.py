"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import bisect
import math
import os
import glob
import sqlite3
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

import policy

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
DB_PATH = Path.home() / ".claude" / "usage.db"
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]

# Higher number = higher priority when choosing a session's primary model.
# fable outranks opus: it is the current top tier (and most expensive in
# pricing.py). Omitting it scored fable-5 below haiku, so any opus/sonnet/haiku
# turn — notably a subagent's — would mislabel a fable-5 session. mythos ranks
# equal to fable, also above opus.
MODEL_PRIORITY = {"fable": 4, "mythos": 4, "opus": 3, "sonnet": 2, "haiku": 1}

# Synthetic message_id prefix for turns minted from `usage.iterations[]` entries
# of type 'advisor_message'. Doubles as the marker that keeps those turns out of
# the session's primary-model vote — matched on the id rather than tool_name so a
# user's own tool named "advisor" can't be mistaken for one.
ADVISOR_ID_PREFIX = "advisor:"

# Output-token estimates for what Claude Code's transcripts under-record (see
# _apply_estimates): subagent lines written mid-stream, advisor calls with no
# usage, and the AgentSummary side calls that are billed but never written.
EST_CHARS_PER_OUTPUT_TOKEN = 2.0   # calibrated against engine /cost across 29 sessions
EST_ADVISOR_OUTPUT_TOKENS = 6158   # median output of 873 recorded advisor_message iterations
AGENT_SUMMARY_INTERVAL_S = 30      # Claude Code forks each running subagent's context this often
AGENT_SUMMARY_INPUT_TOKENS = 300   # fresh input per summary fire
AGENT_SUMMARY_OUTPUT_TOKENS = 60   # output per summary fire
# One aggregated synthetic turn per subagent file: `agent_summary:<file stem>`.
AGENT_SUMMARY_ID_PREFIX = "agent_summary:"


def is_advisor_turn(turn):
    """True if this turn was minted from an advisor_message iteration."""
    return (turn.get("message_id") or "").startswith(ADVISOR_ID_PREFIX)


def is_agent_summary_turn(turn):
    """True if this turn is the synthetic per-subagent AgentSummary estimate."""
    return (turn.get("message_id") or "").startswith(AGENT_SUMMARY_ID_PREFIX)


def _model_priority(model):
    """Return a priority score for a model name (higher = more capable)."""
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def get_db(db_path=DB_PATH):
    # Ensure the parent directory exists — on a fresh install or CI runner
    # ~/.claude may not yet exist, and sqlite3.connect needs the parent dir.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL lets readers (the CLI, the dashboard, any external tool that opens the
    # file — "the hub") query while a scan is writing, instead of hitting
    # "database is locked". journal_mode is a persistent property of the DB file,
    # so asserting it on every connect is idempotent. synchronous=NORMAL is the
    # safe, recommended pairing for WAL; busy_timeout makes the rare writer/writer
    # overlap wait briefly rather than erroring. ``:memory:`` DBs ignore WAL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            agent           TEXT DEFAULT 'claude',
            topic           TEXT,
            topic_source    TEXT
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            cache_creation_5m_tokens INTEGER DEFAULT 0,
            cache_creation_1h_tokens INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT,
            duration_ms             INTEGER DEFAULT 0,
            stop_reason             TEXT,
            service_tier            TEXT,
            inference_geo           TEXT,
            is_sidechain            INTEGER DEFAULT 0,
            is_compact_summary      INTEGER DEFAULT 0,
            agent                   TEXT DEFAULT 'claude',
            reasoning_tokens        INTEGER DEFAULT 0,
            effort                  TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER,
            source  TEXT DEFAULT 'claude'
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Subagent dispatch metadata (ported from upstream claude-usage).
        -- One row per Task/Agent dispatch, keyed by the agentId Claude Code writes
        -- on both the subagent's turns and the parent's closing tool_result. The
        -- aggregate stats (tokens / duration / tool uses) come from that tool_result.
        -- Only the Claude source ever writes here; Codex has no subagent concept.
        CREATE TABLE IF NOT EXISTS agents (
            agent_id              TEXT PRIMARY KEY,
            agent_type            TEXT,
            dispatched_in_session TEXT,
            completed_at          TEXT,
            status                TEXT,
            total_tokens          INTEGER,
            total_duration_ms     INTEGER,
            tool_use_count        INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(agent_type);
    """)
    # Add columns if upgrading from older schema.
    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(turns)").fetchall()
    }
    migrations = {
        "message_id": "TEXT",
        "cache_creation_5m_tokens": "INTEGER DEFAULT 0",
        "cache_creation_1h_tokens": "INTEGER DEFAULT 0",
        "duration_ms": "INTEGER DEFAULT 0",
        "stop_reason": "TEXT",
        "service_tier": "TEXT",
        "inference_geo": "TEXT",
        # usage.speed: "fast" bills 2x (pricing.PRICE_MULT_SQL). Filled for old
        # rows by ClaudeSource.backfill_speed.
        "speed": "TEXT",
        "is_sidechain": "INTEGER DEFAULT 0",
        "is_compact_summary": "INTEGER DEFAULT 0",
        "tool_calls": "TEXT",
        # Multi-agent ingestion. `agent` tags the source ('claude' /
        # 'codex'); DEFAULT 'claude' backfills every pre-existing row correctly.
        # `reasoning_tokens` is informational (Codex reasoning is already inside
        # output_tokens and is never billed separately).
        "agent": "TEXT DEFAULT 'claude'",
        "reasoning_tokens": "INTEGER DEFAULT 0",
        # Subagent attribution: the dispatched-agent id carried on a subagent's own
        # turns (matches agents.agent_id). NULL on ordinary main-chain turns.
        "agent_id": "TEXT",
        # Reasoning effort level ('high'/'medium'/'low'/'xhigh'). Claude Code
        # writes it on assistant records from 2.1.215; Codex carries it in
        # turn_context/thread_settings. NULL on migrated rows and on turns from
        # older versions — queries normalise that to 'unknown'. Existing rows are
        # never rewritten (INSERT OR IGNORE), so backfill = drop the DB + rescan.
        "effort": "TEXT",
        # Skill attribution: the skill that was active when Claude Code produced this
        # turn ('next-task', 'local-delegate', ...), read from the record's top-level
        # `attributionSkill`. NULL/'' on turns produced outside any skill and on rows
        # migrated from before the column — ClaudeSource.backfill_skill fills those in
        # once, since INSERT OR IGNORE never rewrites an existing row.
        "skill": "TEXT",
        # MCP/plugin attribution: `attributionMcpServer` / `attributionMcpTool` /
        # `attributionPlugin` sit on the assistant turn that consumed that MCP
        # tool's result (idea from upstream claude-usage). Orthogonal to the
        # main/subagent/skill actor split — a subagent turn can carry one too.
        # Filled for old rows by ClaudeSource.backfill_mcp.
        "mcp_server": "TEXT",
        "mcp_tool": "TEXT",
        "plugin": "TEXT",
        # 1 = some token count on this row is an estimate (truncated subagent
        # output, unrecorded advisor call, or the synthetic agent_summary row).
        "estimated": "INTEGER DEFAULT 0",
    }
    for col, spec in migrations.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {spec}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(agent_id)")
    # sessions also carry an `agent` column (same additive pattern).
    session_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "agent" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN agent TEXT DEFAULT 'claude'")
    # Session topic (from custom-title / ai-title records; added in a later schema
    # version). The one-time backfill of pre-existing sessions is driven by
    # backfill_topics_once() via the schema_meta 'topic_backfill_done' marker (not by
    # the column-add event), so it also covers DBs that gained the column from an
    # earlier build that predated the backfill.
    if "topic" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN topic TEXT")
    # topic_source records provenance ('custom' / 'ai') so an incrementally
    # appended ai-title can never clobber a user-set custom-title (custom always
    # wins; ai only overwrites a NULL topic or a previous ai-title).
    if "topic_source" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN topic_source TEXT")
    # processed_files has a `source` column so the multi-source engine keys
    # incremental bookkeeping per (path, source) — two sources that discover the
    # same path don't clobber each other (default 'claude' for legacy rows).
    pf_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(processed_files)").fetchall()
    }
    if "source" not in pf_cols:
        conn.execute("ALTER TABLE processed_files ADD COLUMN source TEXT DEFAULT 'claude'")
    # Conditional unique index: only dedup non-null message IDs
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)
    conn.commit()


def extract_tool_calls(content):
    """Return a list of {name, input} dicts for every tool_use block in a
    message's content list. Captures *all* tool calls in a turn (an assistant
    message can fire several at once). Inputs are reduced to allowlisted
    metadata by policy.redact_calls unless the raw-input opt-in is on."""
    calls = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                calls.append({
                    "name": item.get("name"),
                    "input": item.get("input") or {},
                })
    return policy.redact_calls(calls)


def _project_name_from_path(path):
    # Normalize to forward slashes, take last 2 components
    parts = path.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


@lru_cache(maxsize=4096)
def worktree_main_root(cwd):
    """The main checkout a linked git worktree belongs to, or None.

    Idea from upstream claude-usage, deliberately narrower: only a *linked worktree*
    (whose private git dir has a `commondir` file) is folded into its main repo.
    Ordinary repos — and sessions in their subfolders — keep the path heuristic,
    so no historical project name changes. Claude Code's own
    `<repo>/.claude/worktrees/<name>` layout is recognised from the path alone,
    which keeps worktrees that have since been deleted grouped too. Paths are
    normalised, never resolve()d: on Windows that follows junctions and would
    rename the repo itself.
    """
    norm = cwd.replace("\\", "/")
    marker = "/.claude/worktrees/"
    if marker in norm:
        return norm.split(marker, 1)[0]
    p = Path(cwd)
    for candidate in (p, *p.parents):
        git = candidate / ".git"
        try:
            if git.is_dir():
                return None  # a main checkout, not a worktree
            if not git.is_file():
                continue
            content = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        git_dir = Path(os.path.normpath(candidate / content[len("gitdir:"):].strip()))
        try:
            rel = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        except OSError:
            return None  # a submodule, or a worktree whose git dir is gone
        common = Path(os.path.normpath(git_dir / rel))
        return str(common.parent) if common.name == ".git" else None
    return None


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path (worktrees fold into their repo)."""
    if not cwd:
        return "unknown"
    return _project_name_from_path(worktree_main_root(cwd) or cwd)


# ── Subagent dispatch attribution (ported from upstream claude-usage) ──────────
# Upstream carries a dedicated turns.is_subagent flag; here turns.is_sidechain plays
# that role, so is_subagent_record maps the broader detection ONTO is_sidechain
# rather than adding a duplicate column. agent_id capture and the agents dispatch
# table follow upstream.

def is_subagent_record(record, source_path=""):
    """True if a record belongs to a dispatched subagent (Task/Agent tool).

    Detected three ways: the explicit ``isSidechain`` flag, an ``agentId`` on the
    record (or its ``data`` wrapper), or a transcript path under a ``subagents``
    directory (Claude Code writes one jsonl per subagent). This drives the existing
    ``is_sidechain`` column (upstream's is_subagent): it is broader than the bare
    ``isSidechain`` flag, so agentId/subagent-path turns are excluded from a session's
    primary-model vote too.
    """
    if record.get("isSidechain"):
        return True
    if record.get("agentId"):
        return True
    data = record.get("data")
    if isinstance(data, dict) and data.get("agentId"):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp


def record_agent_id(record):
    """Pull the subagent id off a record, if any (top-level or data wrapper)."""
    agent_id = record.get("agentId")
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = data.get("agentId")
    return agent_id


def extract_agent_dispatch(record):
    """Pull subagent identity from a parent's tool_result record.

    Claude Code writes a ``toolUseResult`` dict on the user-side record that closes
    out an Agent/Task tool invocation. It carries ``agentId`` (matching the subagent
    jsonl's records) and usually ``agentType`` (the human-readable type such as
    'general-purpose' or 'Explore') plus aggregate stats. Async dispatches often only
    carry ``agentId`` — those must still be captured (agent_type NULL, displayed as
    'unknown' downstream) rather than silently dropped. Returns None for any record
    that isn't such a dispatch result.
    """
    if record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = tur.get("agentId")
    if not agent_id:
        return None
    return {
        "agent_id": agent_id,
        "agent_type": tur.get("agentType"),
        "dispatched_in_session": record.get("sessionId"),
        "completed_at": record.get("timestamp", ""),
        "status": tur.get("status"),
        "total_tokens": tur.get("totalTokens"),
        "total_duration_ms": tur.get("totalDurationMs"),
        "tool_use_count": tur.get("totalToolUseCount"),
    }


def upsert_agents(conn, agents):
    """Insert or update agent dispatch metadata. Last non-NULL write wins per field.

    Every field COALESCEs against the stored row rather than overwriting it, because
    the same agent is described by two independent records that each know only part
    of the story: the parent's tool_result (agentId + usually agentType + totals) and
    the agent's own ``subagents/agent-<id>.meta.json`` (agentType, always). A
    tool_result that omits ``agentType`` — the case for roughly half of a real
    corpus, and the reason those rows read NULL — must not erase a type another
    record already supplied. None of these fields is ever meaningfully *set* to NULL,
    so "don't overwrite known with unknown" loses nothing and makes the sweep in
    ClaudeSource.sync_agents durable across later rescans.
    """
    if not agents:
        return
    conn.executemany("""
        INSERT INTO agents
            (agent_id, agent_type, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            agent_type            = COALESCE(excluded.agent_type, agents.agent_type),
            dispatched_in_session = COALESCE(excluded.dispatched_in_session, agents.dispatched_in_session),
            completed_at          = COALESCE(excluded.completed_at, agents.completed_at),
            status                = COALESCE(excluded.status, agents.status),
            total_tokens          = COALESCE(excluded.total_tokens, agents.total_tokens),
            total_duration_ms     = COALESCE(excluded.total_duration_ms, agents.total_duration_ms),
            tool_use_count        = COALESCE(excluded.tool_use_count, agents.tool_use_count)
    """, [
        (a["agent_id"], a["agent_type"], a.get("dispatched_in_session"),
         a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def _meta_get(conn, key):
    """Read a value from the schema_meta key/value table (None if absent)."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    """Upsert a value into the schema_meta key/value table."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, value))


def _extract_title(record):
    """Extract a session title from a custom-title or ai-title record."""
    rtype = record.get("type")
    if rtype == "custom-title":
        return record.get("customTitle")
    if rtype == "ai-title":
        return record.get("aiTitle")
    return None


def _backfill_topics(conn, jsonl_files):
    """One-time backfill of topics for a DB created before topic support.

    Transcript files scanned before the topic column existed are already in
    processed_files, so an incremental scan skips them and never sees the
    custom-title / ai-title records they already contain. Re-read just those
    records (turns are left untouched, so token totals cannot drift) and set the
    topic for any session that doesn't have one yet. Returns the number of
    sessions filled.
    """
    needing = {r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE topic IS NULL OR topic = ''")}
    if not needing:
        return 0

    titles = {}          # session_id -> chosen title
    sources = {}         # session_id -> 'custom' / 'ai'
    has_custom = set()   # sessions whose topic came from a custom-title record
    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: only title records carry the substring
                    # "title" (in their "custom-title" / "ai-title" type), so we
                    # skip JSON-parsing the ~99% of lines that are turns.
                    if "title" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    title = _extract_title(record)
                    if not title:
                        continue
                    sid = record.get("sessionId")
                    if sid not in needing:
                        continue
                    # custom-title wins; ai-title only if no custom-title seen.
                    if record.get("type") == "custom-title":
                        titles[sid] = title
                        sources[sid] = "custom"
                        has_custom.add(sid)
                    elif sid not in has_custom:
                        titles.setdefault(sid, title)
                        sources.setdefault(sid, "ai")
        except Exception as e:
            print(f"  Warning: error reading {filepath}: {e}")

    for sid, title in titles.items():
        conn.execute(
            "UPDATE sessions SET topic = ?, topic_source = ? WHERE session_id = ? "
            "AND (topic IS NULL OR topic = '')", (title, sources.get(sid), sid))
    conn.commit()
    return len(titles)


def backfill_topics_once(conn, jsonl_files, verbose=True, dirs_found=True):
    """Run _backfill_topics exactly once, ever, gated by a schema_meta marker.

    Called by a source (see sources/claude.py) before its normal incremental
    walk. Codex has no title records, so only the Claude source wires this in —
    Codex sessions simply keep topic NULL. Safe to call on every scan: a no-op
    once the marker is set.

    ``dirs_found`` reports whether any Claude transcript directory actually
    existed on disk. If none did (missing/unreadable ~/.claude/projects, e.g. a
    fresh machine or a misconfigured override), the marker is deliberately left
    unset so a later scan — once the directory shows up — retries the backfill
    instead of silently skipping it forever.
    """
    if _meta_get(conn, "topic_backfill_done") == "1":
        return 0
    if not dirs_found:
        return 0
    filled = _backfill_topics(conn, jsonl_files)
    _meta_set(conn, "topic_backfill_done", "1")
    conn.commit()
    if verbose and filled:
        print(f"Backfilled topic for {filled} existing session(s).")
    return filled


# The `message_id IS NOT NULL AND message_id != ''` terms look redundant next to
# `message_id = ?` but are load-bearing: idx_turns_message_id is a PARTIAL index
# with exactly that predicate, and SQLite only uses a partial index when the query
# restates its terms (it cannot prove a bound parameter is non-empty). Without
# them every UPDATE degrades to a full table scan — 13 minutes vs 3 seconds over a
# real 64k-turn corpus. A module constant so the test can assert the query plan.
def backfill_worktree_projects_once(conn, verbose=True):
    """One-time rename of sessions scanned before worktrees folded into their repo.

    Recomputes project_name from each session's first cwd — the same rule a new
    scan uses — and rewrites only rows whose name actually changes. Outside a
    worktree project_name_from_cwd is unchanged, so only worktree sessions move.
    """
    if _meta_get(conn, "worktree_project_backfill_done") == "1":
        return 0
    rows = conn.execute("""
        SELECT s.session_id, s.project_name, (
            SELECT t.cwd FROM turns t
            WHERE t.session_id = s.session_id AND t.cwd IS NOT NULL AND t.cwd != ''
            ORDER BY t.timestamp, t.id LIMIT 1) AS cwd
        FROM sessions s
    """).fetchall()
    updates = [(new, sid) for sid, old, cwd in rows
               if cwd and (new := project_name_from_cwd(cwd)) != old]
    conn.executemany("UPDATE sessions SET project_name = ? WHERE session_id = ?", updates)
    _meta_set(conn, "worktree_project_backfill_done", "1")
    conn.commit()
    if verbose and updates:
        print(f"Folded {len(updates)} worktree session(s) into their main repo's project.")
    return len(updates)


def backfill_column_sql(column):
    """The one-time backfill UPDATE for a turns column, with the index terms intact.

    ``column`` is never user input — callers pass a literal from this module — so
    interpolating it is safe; the *values* stay bound parameters.
    """
    return (
        f"UPDATE turns SET {column} = ? WHERE message_id = ? "
        "AND message_id IS NOT NULL AND message_id != '' "
        f"AND ({column} IS NULL OR {column} = '')"
    )


BACKFILL_EFFORT_SQL = backfill_column_sql("effort")


def force_reparse_once(conn, marker, source, verbose=True, why="to pick up advisor turns"):
    """Drop a source's ``processed_files`` rows exactly once, so the next scan
    re-reads every transcript instead of skipping unchanged ones.

    The effort/topic backfills can UPDATE existing rows in place, but advisor
    turns are *new rows* that only a re-parse can produce — and an incremental
    scan skips a file whose mtime/line count is unchanged, so an existing DB
    would silently keep zero advisor turns and stay undercounted forever.

    Re-parsing is safe to repeat: turn inserts are INSERT OR IGNORE against the
    unique message_id index, and session totals are recomputed from ``turns``
    afterwards, so no row is duplicated and no total drifts. It is merely slow
    (a few seconds for a couple of thousand transcripts), which is why the marker
    keeps it to once ever.
    """
    if _meta_get(conn, marker) == "1":
        return 0
    before = conn.total_changes
    conn.execute("DELETE FROM processed_files WHERE COALESCE(source, 'claude') = ?",
                 (source,))
    cleared = conn.total_changes - before
    _meta_set(conn, marker, "1")
    conn.commit()
    if verbose and cleared:
        print(f"  One-time re-parse of {cleared} {source} transcript(s) "
              f"{why}.")
    return cleared


def backfill_column_once(conn, marker, column, pairs_fn, verbose=True, label=None):
    """One-time backfill of a single turns column, gated by a schema_meta marker.

    Generalised from backfill_effort_once (which is now a thin wrapper) so a second
    late-arriving column — turns.skill — reuses the same guard rather than copying
    it. See backfill_effort_once for why an UPDATE, not a rescan, is the only fix.
    """
    if _meta_get(conn, marker) == "1":
        return 0
    # Nothing to fill (the common case: a fresh DB, where the walk that follows
    # parses the value inline). Mark done without reading the corpus — pairs_fn is
    # a full transcript read, and on a cold cache that is minutes for no rows.
    if not conn.execute(
        f"SELECT 1 FROM turns WHERE {column} IS NULL OR {column} = '' LIMIT 1"
    ).fetchone():
        _meta_set(conn, marker, "1")
        conn.commit()
        return 0
    before = conn.total_changes
    conn.executemany(
        backfill_column_sql(column),
        ((value, mid) for mid, value in pairs_fn() if mid and value),
    )
    filled = conn.total_changes - before
    _meta_set(conn, marker, "1")
    conn.commit()
    if verbose and filled:
        print(f"Backfilled {label or column} for {filled} existing turn(s).")
    return filled


# Advisor turns carry a synthetic message_id (`advisor:<parent id>:<idx>`) that
# appears in no transcript, so a message_id-keyed backfill can never reach them —
# they would keep an empty `skill` forever and pool into the "main thread" bucket
# while their own parent sits under a skill, splitting one call across two buckets.
# Recovering the parent id: strip the `advisor:` prefix (8 chars), then the trailing
# `:<idx>`. rtrim of digits stops at the ':' even when the parent id itself ends in
# digits, since ':' is not in the strip set.
PROPAGATE_ADVISOR_COLUMN_SQL = """
    UPDATE turns SET {col} = (
        SELECT p.{col} FROM turns p
        WHERE p.message_id = rtrim(rtrim(substr(turns.message_id, 9), '0123456789'), ':')
          AND p.session_id = turns.session_id
    )
    WHERE message_id LIKE 'advisor:%'
      AND ({col} IS NULL OR {col} = '')
"""


def propagate_advisor_column(conn, column):
    """Copy a column from each advisor turn's parent turn (see the SQL above).

    Mirrors the parse-time contract, where an advisor turn is minted as a copy of its
    parent (``dict(turn, ...)``) and so inherits effort/is_sidechain/skill; this is
    the same inheritance applied after the fact to rows that predate the column.
    Returns rows updated.
    """
    before = conn.total_changes
    conn.execute(PROPAGATE_ADVISOR_COLUMN_SQL.format(col=column))
    return conn.total_changes - before


def backfill_effort_once(conn, marker, pairs_fn, verbose=True):
    """One-time backfill of turns.effort, gated by a schema_meta marker.

    Same problem as _backfill_topics: files scanned before the column existed are
    already in processed_files, and even a forced re-parse could not help — turn
    inserts are INSERT OR IGNORE, so an existing row is never rewritten. Each
    source supplies ``pairs_fn() -> iterable of (message_id, effort)`` and this
    UPDATEs only rows that still have no effort, so token columns can't drift.

    ``marker`` is per-source ('effort_backfill_done_claude' / '..._codex') so one
    source finding nothing doesn't mark the other as done. Returns rows filled.
    """
    if _meta_get(conn, marker) == "1":
        return 0
    # Nothing to fill (the common case: a fresh DB, where the walk that follows
    # parses effort inline). Mark done without reading the corpus — pairs_fn is a
    # full transcript read, and on a cold cache that is minutes for no rows.
    if not conn.execute(
        "SELECT 1 FROM turns WHERE effort IS NULL OR effort = '' LIMIT 1"
    ).fetchone():
        _meta_set(conn, marker, "1")
        conn.commit()
        return 0
    before = conn.total_changes
    conn.executemany(
        BACKFILL_EFFORT_SQL,
        ((effort, mid) for mid, effort in pairs_fn() if mid and effort),
    )
    filled = conn.total_changes - before
    _meta_set(conn, marker, "1")
    conn.commit()
    if verbose and filled:
        print(f"Backfilled effort for {filled} existing turn(s).")
    return filled


def _parse_file(filepath, start_line=0, agents_out=None, info=None):
    """Parse a JSONL file and return (session_metas, turns, line_count).

    ``info``, if a dict, collects per-message estimate inputs (see _note_message).

    ``agents_out``, if a dict is passed, is populated in place with subagent
    dispatch metadata keyed by agent_id (the caller upserts it into the ``agents``
    table). Left as an out-param so the 3-tuple return — and its many callers /
    tests — stays unchanged; only the source wrappers that persist agents opt in.

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).

    start_line > 0 restricts parsing to records *after* that line number, used
    for incremental rescans of a file that has grown. The returned line_count
    is always the file's true total line count (so processed_files records the
    full length, not just the newly-parsed tail). Both the full-scan and
    incremental paths go through this one function, so every turn — regardless
    of which path produced it — carries the identical set of fields.
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                if line_count <= start_line:
                    continue
                complete = line.endswith("\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if not complete:
                        # Trailing line still being written: leave it out of
                        # line_count so the next incremental scan re-reads it.
                        line_count -= 1
                        break
                    continue

                if info is not None and record.get("timestamp"):
                    info["events"].append(record["timestamp"])

                rtype = record.get("type")
                if rtype not in ("assistant", "user", "custom-title", "ai-title"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                # Extract session title from title records. These carry a
                # sessionId but no timestamp/cwd, so they never create a "real"
                # session on their own — upsert_sessions skips the phantom INSERT
                # for a meta-only entry with no first_timestamp.
                title = _extract_title(record)
                if title:
                    if session_id not in session_meta:
                        session_meta[session_id] = {
                            "session_id": session_id,
                            "project_name": "unknown",
                            "first_timestamp": "",
                            "last_timestamp": "",
                            "git_branch": "",
                            "model": None,
                            "topic": None,
                            "topic_source": None,
                        }
                    meta = session_meta[session_id]
                    # custom-title always wins; ai-title only if no custom-title set
                    # (locally, within this file) or the prior title was itself ai.
                    if rtype == "custom-title":
                        meta["topic"] = title
                        meta["topic_source"] = "custom"
                    elif rtype == "ai-title" and meta.get("topic_source") in (None, "ai"):
                        meta["topic"] = title
                        meta["topic_source"] = "ai"
                    continue

                # Subagent dispatch metadata rides on the parent's user-side
                # tool_result record (toolUseResult.agentId/agentType/...). Capture
                # it before the token-bearing branch below (user records never
                # become turns).
                if agents_out is not None:
                    dispatch = extract_agent_dispatch(record)
                    if dispatch is not None:
                        agents_out[dispatch["agent_id"]] = dispatch

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                        "topic": None,
                        "topic_source": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch
                    # A title record may have created this session_meta entry first,
                    # leaving project_name at the "unknown" placeholder (title records
                    # carry no cwd). The first real record supplies the true project.
                    if cwd and meta["project_name"] == "unknown":
                        meta["project_name"] = project_name_from_cwd(cwd)

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")
                    if info is not None and message_id:
                        _note_message(info, message_id, msg, usage, record)

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_creation_detail = usage.get("cache_creation", {})
                    if not isinstance(cache_creation_detail, dict):
                        cache_creation_detail = {}
                    cache_creation_5m = cache_creation_detail.get("ephemeral_5m_input_tokens", 0) or 0
                    cache_creation_1h = cache_creation_detail.get("ephemeral_1h_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract every tool call in this turn (with full inputs)
                    tool_calls = extract_tool_calls(msg.get("content", []))
                    tool_name = tool_calls[0]["name"] if tool_calls else None

                    if model:
                        session_meta[session_id]["model"] = model

                    turn = {
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "cache_creation_5m_tokens": cache_creation_5m,
                        "cache_creation_1h_tokens": cache_creation_1h,
                        "tool_name": tool_name,
                        "cwd": cwd,
                        "message_id": message_id,
                        "duration_ms": record.get("durationMs", 0) or 0,
                        "stop_reason": record.get("stopReason") or msg.get("stop_reason") or "",
                        "service_tier": usage.get("service_tier", "") or "",
                        "inference_geo": usage.get("inference_geo", "") or "",
                        "speed": usage.get("speed", "") or "",
                        # is_sidechain carries upstream's full subagent detection
                        # (isSidechain flag OR agentId OR a /subagents/ transcript path),
                        # not just the bare flag — see is_subagent_record.
                        "is_sidechain": 1 if is_subagent_record(record, filepath) else 0,
                        "is_compact_summary": 1 if record.get("isCompactSummary") else 0,
                        "agent_id": record_agent_id(record),
                        # Reasoning effort, written top-level on assistant records
                        # from Claude Code 2.1.215 (absent before → '').
                        "effort": record.get("effort") or "",
                        # Skill attribution, written top-level next to `effort`.
                        "skill": record.get("attributionSkill") or "",
                        "mcp_server": record.get("attributionMcpServer") or "",
                        "mcp_tool": record.get("attributionMcpTool") or "",
                        "plugin": record.get("attributionPlugin") or "",
                        "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                    }

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

                    # Advisor inferences (Claude Code 2.1.2xx) run *inside* an
                    # assistant message as extra `usage.iterations[]` entries of
                    # type 'advisor_message', on a different (stronger) model named
                    # by the record's top-level `advisorModel`. The envelope's
                    # top-level input/output_tokens count ONLY the 'message'
                    # iterations, so advisor tokens are invisible there — that was a
                    # silent undercount of the whole advisor spend (cache_read /
                    # cache_creation are unaffected: advisor iterations carry none).
                    #
                    # Each one is emitted as its own turn row rather than as extra
                    # columns, because a turn is already "one inference on one
                    # model" and cost is computed per turn from that turn's model —
                    # so fable-5 pricing flows through every existing cost query
                    # unchanged. Synthetic message_id mirrors the Codex dedup
                    # pattern, so re-scans and branch copies stay idempotent.
                    for idx, it in enumerate(usage.get("iterations") or []):
                        if not isinstance(it, dict) or it.get("type") != "advisor_message":
                            continue
                        adv_in = it.get("input_tokens", 0) or 0
                        adv_out = it.get("output_tokens", 0) or 0
                        if adv_in + adv_out == 0:
                            continue
                        adv_turn = dict(
                            turn,
                            # advisorModel is the authority; fall back to the parent
                            # model so an unnamed advisor is still priced, not free.
                            model=record.get("advisorModel") or model,
                            input_tokens=adv_in,
                            output_tokens=adv_out,
                            cache_read_tokens=it.get("cache_read_input_tokens", 0) or 0,
                            cache_creation_tokens=it.get("cache_creation_input_tokens", 0) or 0,
                            cache_creation_5m_tokens=0,
                            cache_creation_1h_tokens=0,
                            # A separate inference on the advisor's model: the
                            # parent's fast mode doesn't carry over (and Fable,
                            # the usual advisor, has no fast mode).
                            speed="",
                            tool_name="advisor",
                            tool_calls=None,
                            message_id="%s%s:%d" % (ADVISOR_ID_PREFIX, message_id, idx),
                        )
                        # is_sidechain / effort are inherited from the parent turn on
                        # purpose: advisor cost is main-chain cost when the parent is,
                        # and it is induced by the parent's effort level, so it must
                        # land in the same effort bucket.
                        if message_id:
                            seen_messages[adv_turn["message_id"]] = adv_turn
                        else:
                            # No parent id to key on: the synthetic id would collide
                            # across messages, so follow the parent into the un-deduped
                            # list rather than silently dropping all but one.
                            adv_turn["message_id"] = ""
                            turns_no_id.append(adv_turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, line_count


def _new_info():
    """Estimate inputs from a whole-file parse: per-message and every record time."""
    return {"msgs": {}, "events": []}


def _note_message(info, mid, msg, usage, record):
    """Accumulate one record into ``info["msgs"][mid]`` (chars sum over ALL of a
    message's lines; adv_iter is true if ANY line carried an advisor_message
    iteration; open/adv_model come from the last line seen)."""
    i = info["msgs"].setdefault(
        mid, {"chars": 0, "adv_block": False, "adv_iter": False, "adv_model": ""})
    content = msg.get("content")
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        ty = b.get("type")
        if ty == "text":
            i["chars"] += len(b.get("text") or "")
        elif ty == "thinking":
            i["chars"] += len(b.get("thinking") or "")
        elif ty in ("tool_use", "server_tool_use"):
            i["chars"] += (len(json.dumps(b.get("input"), ensure_ascii=False))
                           + len(b.get("name") or ""))
            if ty == "server_tool_use" and b.get("name") == "advisor":
                i["adv_block"] = True
    i["open"] = "stop_reason" in msg and msg["stop_reason"] is None
    if any(isinstance(it, dict) and it.get("type") == "advisor_message"
           for it in usage.get("iterations") or []):
        i["adv_iter"] = True
    if record.get("advisorModel"):
        i["adv_model"] = record["advisorModel"]


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _apply_estimates(filepath, turns, full_turns, info):
    """Patch ``turns`` in place and append synthetic estimate turns.

    ``full_turns`` / ``info`` come from a whole-file parse, so an incremental scan
    sees every line of a message and the whole subagent timeline. insert_turns'
    upsert keeps re-scans idempotent and never lets an estimate beat a real row.
    """
    extra = []
    for t in turns:
        i = info["msgs"].get(t.get("message_id") or "")
        if not i:
            continue
        # (a) mid-stream subagent line: output_tokens is a stream-start snapshot.
        if i["open"]:
            est = round(i["chars"] / EST_CHARS_PER_OUTPUT_TOKEN)
            if est > t["output_tokens"]:
                t["output_tokens"] = est
                t["estimated"] = 1
        # (b) advisor call whose inference never reached usage.iterations[] on any
        # of the message's records.
        if i["adv_block"] and not i["adv_iter"]:
            extra.append(dict(
                t,
                model=i["adv_model"] or t["model"],
                input_tokens=t["input_tokens"] + t["cache_read_tokens"] + t["cache_creation_tokens"],
                output_tokens=EST_ADVISOR_OUTPUT_TOKENS,
                cache_read_tokens=0, cache_creation_tokens=0,
                cache_creation_5m_tokens=0, cache_creation_1h_tokens=0,
                speed="", tool_name="advisor", tool_calls=None,
                message_id="%s%s:est" % (ADVISOR_ID_PREFIX, t["message_id"]),
                estimated=1))

    # (c) AgentSummary: Claude Code forks a still-running subagent's context every
    # 30 s from its start, if its transcript changed since the last fire. A fire
    # counts at tick t0+k*30 (k>=1) only up to the agent's last activity, when any
    # record (user/tool_result too) landed in that tick's window, priced as the
    # latest assistant message's context at that tick re-read from cache.
    # Aggregated into ONE turn per subagent file so turn counts don't inflate.
    msgs = []
    for t in full_turns:
        ts = _parse_ts(t.get("timestamp"))
        if (t.get("is_sidechain") and t.get("message_id") and t.get("model")
                and ts is not None and not is_advisor_turn(t)):
            msgs.append((ts, t))
    events = [e for e in map(_parse_ts, info["events"]) if e is not None]
    if msgs and events:
        msgs.sort(key=lambda m: m[0])
        times = [m[0] for m in msgs]
        t0, t_end = min(events), max(events)
        buckets = {max(1, math.ceil((e - t0) / AGENT_SUMMARY_INTERVAL_S)) for e in events}
        n = ctx = 0
        for k in sorted(buckets):
            tick = t0 + k * AGENT_SUMMARY_INTERVAL_S
            j = bisect.bisect_right(times, tick) - 1
            if tick > t_end or j < 0:
                continue
            m = msgs[j][1]
            n += 1
            ctx += m["input_tokens"] + m["cache_read_tokens"] + m["cache_creation_tokens"]
        if n:
            last = msgs[-1][1]
            extra.append(dict(
                last,
                input_tokens=n * AGENT_SUMMARY_INPUT_TOKENS,
                output_tokens=n * AGENT_SUMMARY_OUTPUT_TOKENS,
                cache_read_tokens=ctx,
                cache_creation_tokens=0, cache_creation_5m_tokens=0, cache_creation_1h_tokens=0,
                speed="", tool_name=None, tool_calls=None, stop_reason="",
                duration_ms=0, is_sidechain=1, is_subagent=1, estimated=1,
                message_id="%s%s:%s" % (AGENT_SUMMARY_ID_PREFIX, last["session_id"],
                                         Path(filepath).stem)))
    turns.extend(extra)


def parse_jsonl_file(filepath, start_line=0, agents_out=None):
    """_parse_file plus the transcript-gap estimates (see _apply_estimates).

    Estimates need whole-message / whole-timeline context, so an incremental
    parse (start_line > 0) re-reads the file from line 1 for them. A subagent
    file is re-read even when the new lines hold no assistant turn: a user-only
    append still moves the AgentSummary estimate.
    """
    info = _new_info()
    if start_line == 0:
        metas, turns, n = _parse_file(filepath, 0, agents_out, info)
        full = list(turns)
    else:
        metas, turns, n = _parse_file(filepath, start_line, agents_out)
        if n <= start_line or not (turns or is_subagent_record({}, filepath)):
            return metas, turns, n
        _, full, _ = _parse_file(filepath, 0, None, info)
    _apply_estimates(filepath, turns, full, info)
    return metas, turns, n


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict, Counter

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
        "agent": "claude",
    })
    session_model_counts = defaultdict(Counter)

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        # All turns of a session share one agent; carry it onto the rollup.
        s["agent"] = t.get("agent", "claude")
        # Only MAIN-CHAIN turns vote on the session's primary model. A subagent
        # (sidechain) often runs on a different model than the session it serves
        # (e.g. a fable-5 session spawning an opus Task); counting its turns would
        # mislabel the session. Sidechain tokens are still summed for cost — they
        # just don't define the label. reconcile_session_models is authoritative.
        # Advisor turns don't vote either: they are an auxiliary inference on a
        # different model, and fable-5 outranks opus in MODEL_PRIORITY, so one
        # advisor call would otherwise relabel an entire Opus session as fable-5.
        if t["model"] and t.get("is_sidechain") != 1 and not is_advisor_turn(t) and not is_agent_summary_turn(t):
            session_model_counts[t["session_id"]][t["model"]] += 1

    for sid, counts in session_model_counts.items():
        if counts:
            session_stats[sid]["model"] = counts.most_common(1)[0][0]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        merged = {**meta, **stats}
        # Turns are the source of truth for agent; a turn-less (meta-only) session
        # has none yet, so fall back to the source's meta tag instead of the
        # 'claude' default baked into session_stats.
        if stats["turn_count"] == 0:
            merged["agent"] = meta.get("agent", merged.get("agent", "claude"))
        result.append(merged)
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        # A session seen only via a title record (custom-title / ai-title carry a
        # sessionId but no timestamp) has no real content. Don't let it INSERT a
        # phantom, token-less row; if the session already exists it still falls
        # through to the UPDATE below and sets its topic.
        if existing is None and not s.get("first_timestamp"):
            continue

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     agent, topic, topic_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], s.get("agent", "claude"),
                s.get("topic"), s.get("topic_source")
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            # Keep the highest-priority model (e.g. opus over haiku from subagents)
            existing_row = conn.execute(
                "SELECT model, topic, topic_source FROM sessions WHERE session_id = ?",
                (s["session_id"],)
            ).fetchone()
            existing_model = existing_row["model"]
            new_model = s["model"]
            if not existing_model:
                # An earlier meta-only insert left model NULL; adopt the first real
                # model now rather than letting the priority tie (0 vs 0) keep NULL.
                model_to_set = new_model
            elif _model_priority(new_model) > _model_priority(existing_model):
                model_to_set = new_model
            else:
                model_to_set = existing_model

            # Topic provenance: custom-title always writes. ai-title writes only
            # when there's no existing topic yet, or the existing one was itself
            # an ai-title (so a fresher ai-title can still update it) or the
            # Projects-thread prompt fallback (project_threads.py) — a
            # user-set custom-title is never clobbered by a later ai-title.
            new_topic = s.get("topic")
            new_source = s.get("topic_source")
            existing_topic = existing_row["topic"]
            existing_source = existing_row["topic_source"]
            if new_source == "custom":
                topic_to_set, source_to_set = new_topic, "custom"
            elif new_source == "ai" and (existing_topic is None or existing_source in ("ai", "prompt")):
                topic_to_set, source_to_set = new_topic, "ai"
            else:
                topic_to_set, source_to_set = existing_topic, existing_source

            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?,
                    agent = ?,
                    topic = ?,
                    topic_source = ?
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set, s.get("agent", "claude"),
                topic_to_set, source_to_set, s["session_id"]
            ))


def insert_turns(conn, turns):
    # A real advisor_message turn supersedes the estimate minted while the
    # iteration was missing from the transcript: drop it so nothing double-bills.
    conn.executemany("DELETE FROM turns WHERE message_id = ?", [
        (t["message_id"].rsplit(":", 1)[0] + ":est",)
        for t in turns if is_advisor_turn(t) and not t["message_id"].endswith(":est")])
    conn.executemany("""
        INSERT INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens,
             cache_creation_5m_tokens, cache_creation_1h_tokens,
             tool_name, cwd, message_id, duration_ms, stop_reason,
             service_tier, inference_geo, is_sidechain, is_compact_summary,
             tool_calls, agent, reasoning_tokens, agent_id, effort, skill,
             mcp_server, mcp_tool, plugin, speed, estimated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        -- Claude Code writes a response's usage more than once while it streams;
        -- only the last record is final. A scan landing between them stores the
        -- partial tallies, and the next scan must correct the row rather than be
        -- ignored by the unique message_id index (idea from upstream claude-usage).
        ON CONFLICT(message_id) WHERE message_id IS NOT NULL AND message_id != ''
        DO UPDATE SET
            timestamp                = excluded.timestamp,
            model                    = excluded.model,
            input_tokens             = excluded.input_tokens,
            output_tokens            = excluded.output_tokens,
            cache_read_tokens        = excluded.cache_read_tokens,
            cache_creation_tokens    = excluded.cache_creation_tokens,
            cache_creation_5m_tokens = excluded.cache_creation_5m_tokens,
            cache_creation_1h_tokens = excluded.cache_creation_1h_tokens,
            tool_name                = excluded.tool_name,
            inference_geo            = excluded.inference_geo,
            speed                    = excluded.speed,
            stop_reason              = excluded.stop_reason,
            estimated                = excluded.estimated
        -- Precedence: a real (non-estimated) row always replaces an estimated one,
        -- every token field, and clears the flag even on equal counts. Between two
        -- real rows the larger output wins (the final tally). An estimate never
        -- replaces a real row, except one still mid-stream (no stop_reason yet:
        -- a pre-fix row, or a scan that caught the message open) when it is larger.
        -- Two estimates update whenever the values move (agent_summary grows).
        WHERE (COALESCE(excluded.estimated, 0) = 0
               AND (excluded.output_tokens > turns.output_tokens
                    OR COALESCE(turns.estimated, 0) = 1))
           OR (COALESCE(excluded.estimated, 0) = 1
               AND COALESCE(turns.estimated, 0) = 1
               AND (excluded.output_tokens != turns.output_tokens
                    OR excluded.cache_read_tokens != turns.cache_read_tokens
                    OR excluded.input_tokens != turns.input_tokens))
           OR (COALESCE(excluded.estimated, 0) = 1
               AND COALESCE(turns.estimated, 0) = 0
               AND COALESCE(turns.stop_reason, '') = ''
               AND excluded.output_tokens > turns.output_tokens)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t.get("cache_creation_5m_tokens", 0), t.get("cache_creation_1h_tokens", 0),
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("duration_ms", 0), t.get("stop_reason", ""),
         t.get("service_tier", ""), t.get("inference_geo", ""),
         t.get("is_sidechain", 0), t.get("is_compact_summary", 0),
         t.get("tool_calls"), t.get("agent", "claude"),
         t.get("reasoning_tokens", 0), t.get("agent_id"), t.get("effort") or "",
         t.get("skill") or "",
         t.get("mcp_server") or "", t.get("mcp_tool") or "", t.get("plugin") or "",
         t.get("speed") or "", t.get("estimated", 0))
        for t in turns
    ])


def reconcile_session_totals(conn):
    """Recompute every session's token totals + turn_count from its turns.

    Session rollups are added additively by upsert_sessions while turn inserts use
    INSERT OR IGNORE against the unique message_id index — so on a rescan the rollup
    can drift ahead of the turns actually present. Recomputing from `turns` is the
    single reconcile both the standalone scanner and the multi-source engine
    (sources/) call to prevent that drift. Agent-agnostic: it sums whatever turns a
    session has, regardless of source.
    """
    conn.execute("""
        UPDATE sessions SET
            total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id), 0)
    """)
    conn.commit()


def reconcile_session_models(conn):
    """Recompute every session's primary `model` from its MAIN-CHAIN turns only.

    The per-file upsert path can let a subagent's model win at the session level
    (a fable-5 session that spawns an opus Task would be labeled opus). This is the
    single authoritative pass: among a session's non-sidechain turns, pick the
    highest-priority model (invariant #3), breaking ties by turn count then name
    for determinism. Sessions with only sidechain turns (a subagent file seen
    before its parent) are left untouched until the parent supplies a main-chain
    model. Recomputes ALL sessions, so it self-heals rows written before this fix.
    """
    rows = conn.execute("""
        SELECT session_id, model, COUNT(*) AS n
        FROM turns
        WHERE COALESCE(is_sidechain, 0) = 0
          AND COALESCE(NULLIF(model, ''), '') != ''
          -- Advisor turns are an auxiliary inference on a different model; fable-5
          -- outranks opus here, so one advisor call would relabel the session.
          AND COALESCE(message_id, '') NOT LIKE 'advisor:%'
          AND COALESCE(message_id, '') NOT LIKE 'agent_summary:%'
        GROUP BY session_id, model
    """).fetchall()

    # session_id -> sort key of the winning (priority, count, name) tuple.
    best = {}
    for r in rows:
        sid, model, n = r["session_id"], r["model"], r["n"]
        key = (_model_priority(model), n, model)
        if sid not in best or key > best[sid][0]:
            best[sid] = (key, model)

    for sid, (_, model) in best.items():
        conn.execute(
            "UPDATE sessions SET model = ? WHERE session_id = ?", (model, sid)
        )
    conn.commit()


def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True):
    conn = get_db(db_path)
    init_db(conn)
    policy.migrate_tool_calls(conn, _meta_get, _meta_set)

    if projects_dirs:
        dirs_to_scan = [Path(d) for d in projects_dirs]
    elif projects_dir:
        dirs_to_scan = [Path(projects_dir)]
    else:
        dirs_to_scan = DEFAULT_PROJECTS_DIRS

    jsonl_files = []
    for d in dirs_to_scan:
        if not d.exists():
            continue
        if verbose:
            print(f"Scanning {d} ...")
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {filepath}")

        if is_new:
            # New file: full parse (single read, returns line count)
            agents = {}
            session_metas, turns, line_count = parse_jsonl_file(filepath, agents_out=agents)

            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                upsert_agents(conn, list(agents.values()))
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
                new_files += 1

        else:
            # Updated file: re-parse only the lines added since the last scan.
            # Reuses parse_jsonl_file (via start_line) instead of a second,
            # hand-maintained copy of the parse loop. The old duplicate had
            # already drifted — it dropped the tool_calls field — so any turn
            # ingested incrementally lost its tool-call detail. One parser, one
            # turn shape, no drift.
            old_lines = row["lines"] if row else 0
            agents = {}
            session_metas, turns, line_count = parse_jsonl_file(
                filepath, start_line=old_lines, agents_out=agents)

            if line_count <= old_lines:
                # File didn't grow (mtime changed but no new content)
                conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?",
                             (mtime, filepath))
                conn.commit()
                skipped_files += 1
                continue

            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                upsert_agents(conn, list(agents.values()))
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
            updated_files += 1

        # Record file as processed (line_count already known from the single read)
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    # Recompute session totals from actual turns in DB.
    # This ensures correctness when INSERT OR IGNORE skips duplicate turns
    # but upsert_sessions had already added their tokens additively.
    if new_files or updated_files:
        reconcile_session_totals(conn)
        reconcile_session_models(conn)

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


if __name__ == "__main__":
    import sys
    projects_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--projects-dir" and i + 1 < len(sys.argv[1:]):
            projects_dir = Path(sys.argv[i + 2])
            break
    scan(projects_dir=projects_dir)
