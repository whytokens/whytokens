"""sources/ - pluggable ingestion sources for the profiler.

Two sources exist (Claude Code JSONL and OpenAI Codex), which is what justifies this
minimal seam: no abstraction before a second case needs it.

A **Source** is anything exposing:

    name: str
    discover_files() -> Iterable[path]
    parse_file(path, start_line=0) -> (session_metas, turns, line_count, agents)

where ``turns`` are dicts in the canonical column shape and carry ``agent``, and
``agents`` is a list of subagent-dispatch dicts (empty for sources without a subagent
concept, e.g. Codex) that ``scan_all`` upserts into the ``agents`` table. Every
source funnels into ONE writer + ONE aggregator: ``scan_all`` owns the source-agnostic
machinery (incremental ``processed_files`` walk, ``INSERT OR IGNORE`` dedup, and the
end-of-scan reconcile-from-turns), reusing the vendored ``scanner.py`` helpers so
there is a single implementation of each. Source-specific knowledge (where files live,
how records parse, how tokens map) lives entirely in the per-source module.
"""

import os
import sys
import time

import extensions as _extensions
import policy
import scanner
from config import DB_PATH
from .claude import ClaudeSource
from .codex import CodexSource
from .coordinator import CoordinatorSource

__all__ = ["ClaudeSource", "CodexSource", "CoordinatorSource", "get_sources", "scan_all"]


def get_sources(projects_dir=None):
    """Default source registry. ``projects_dir`` overrides only the Claude dir
    (used by tests / the CLI ``--projects-dir`` flag); Codex uses ``CODEX_PATH``."""
    claude = ClaudeSource(projects_dirs=[projects_dir] if projects_dir else None)
    # Coordinator spend is partial (a rolling cache window); see sources/coordinator.py.
    return [claude, CodexSource(), CoordinatorSource()]


# Live scan progress for the dashboard (one scan runs at a time; see scanrun.py).
PROGRESS = {"source": None, "done": 0, "total": 0, "errors": []}


def _file_error(source, path, exc):
    if len(PROGRESS["errors"]) < 20:  # ponytail: cap so a broken root can't flood the page
        PROGRESS["errors"].append({"source": source, "path": str(path), "error": str(exc)})


RACY_MTIME_SECONDS = 2.0


def _trusted_mtime(mtime):
    """The mtime to record, or -1 when it is too fresh to trust.

    A line appended within the same timestamp tick as the scan leaves mtime unchanged,
    so the skip check would miss it. A fresh stamp is stored as -1, which forces a
    cheap re-check (from the recorded line count) on the next scan."""
    return mtime if time.time() - mtime > RACY_MTIME_SECONDS else -1


def _scan_source(conn, source, verbose):
    """Run one source through the incremental file-walk, writing to ``conn``.

    Mirrors scanner.scan()'s per-file loop but delegates discovery + parsing to the
    source. Parsing lives in exactly one place per source, so there is no duplicated
    *parse* loop to drift out of sync.
    """
    files = source.discover_files()
    new_files = updated_files = skipped_files = total_turns = 0
    sessions_seen = set()
    PROGRESS.update(source=source.name, done=0, total=len(files))

    for filepath in files:
        PROGRESS["done"] += 1
        try:
            mtime = os.path.getmtime(filepath)
        except OSError as e:
            _file_error(source.name, filepath, e)
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ? AND source = ?",
            (filepath, source.name),
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            print(f"  [{source.name}:{'NEW' if is_new else 'UPD'}] {filepath}")

        old_lines = 0 if is_new else (row["lines"] or 0)
        try:  # the vendored parsers swallow read errors and would record the file as done
            open(filepath, "rb").close()
        except OSError as e:  # unreadable: report it, don't mark processed, keep scanning
            _file_error(source.name, filepath, e)
            continue
        session_metas, turns, line_count, agents = source.parse_file(filepath, start_line=old_lines)

        if not is_new and line_count <= old_lines:
            # mtime changed but the file didn't grow — just refresh the stamp.
            conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ? AND source = ?",
                         (_trusted_mtime(mtime), filepath, source.name))
            conn.commit()
            skipped_files += 1
            continue

        if turns or session_metas:
            sessions = scanner.aggregate_sessions(session_metas, turns)
            scanner.upsert_sessions(conn, sessions)
            scanner.insert_turns(conn, turns)
            scanner.upsert_agents(conn, agents)
            for s in sessions:
                sessions_seen.add(s["session_id"])
            total_turns += len(turns)

        if is_new:
            new_files += 1
        else:
            updated_files += 1

        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines, source)
            VALUES (?, ?, ?, ?)
        """, (filepath, _trusted_mtime(mtime), line_count, source.name))
        conn.commit()

    return {
        "new": new_files, "updated": updated_files, "skipped": skipped_files,
        "turns": total_turns, "sessions": len(sessions_seen),
        "_changed": bool(new_files or updated_files),
    }


def scan_all(db_path=DB_PATH, projects_dir=None, verbose=True, sources=None, extensions=None):
    """Scan every registered source into one DB, then reconcile once.

    Returns ``{source_name: stats}``. The reconcile (recompute session totals from
    turns) runs once at the end across all sources so additive rollups can't drift
    past the deduped turns actually inserted.
    """
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)

    if sources is None:
        sources = get_sources(projects_dir)
        # The default scan also runs the user-configured extensions; a caller passing
        # its own sources (tests) opts in with an explicit ``extensions`` list.
        if extensions is None:
            extensions = _extensions.load()
    extensions = extensions or []

    # One-time topic backfill (Claude only — see ClaudeSource.backfill_topics).
    # Runs before the per-source walk so a freshly-upgraded DB gets old sessions'
    # topics filled in on the very next scan, gated by a schema_meta marker so it
    # never repeats.
    scanner.backfill_worktree_projects_once(conn, verbose=verbose)
    # Data policy: rewrite stored tool inputs to allowlisted metadata, once.
    policy.migrate_tool_calls(conn, scanner._meta_get, scanner._meta_set)
    for source in sources:
        if hasattr(source, "backfill_topics"):
            source.backfill_topics(conn)
        # Same one-shot pattern for turns.effort: rows ingested before the column
        # existed can never be fixed by a rescan (turn inserts are INSERT OR
        # IGNORE), so each source rewrites its own historical rows exactly once.
        if hasattr(source, "backfill_effort"):
            source.backfill_effort(conn)
        # turns.skill arrived the same way and is filled by the same guard.
        if hasattr(source, "backfill_skill"):
            source.backfill_skill(conn)
        # turns.mcp_server/mcp_tool/plugin: same guard, one corpus read for all three.
        if hasattr(source, "backfill_mcp"):
            source.backfill_mcp(conn)
        if hasattr(source, "backfill_speed"):
            source.backfill_speed(conn)
        if hasattr(source, "backfill_details"):
            source.backfill_details(conn)
        # Advisor turns are NEW rows, so no in-place UPDATE can add them: an
        # existing DB must re-parse its transcripts once. Runs before the walk so
        # the same scan that clears the cache also re-reads the files.
        if hasattr(source, "backfill_advisor_turns"):
            source.backfill_advisor_turns(conn)

    stats = {}
    changed = False
    for source in sources:
        if verbose:
            print(f"Scanning source: {source.name} ...")
        s = _scan_source(conn, source, verbose)
        # pop unconditionally first — `changed or s.pop(...)` would short-circuit
        # once changed is True and leak the private _changed key into stats.
        src_changed = s.pop("_changed", False)
        changed = changed or src_changed
        stats[source.name] = s

    # Subagent identity sweep (Claude only). Runs AFTER the walk — unlike the
    # backfills above — because it enriches rows the walk itself may have just
    # created, and it is unmarked/idempotent rather than one-shot: see
    # ClaudeSource.sync_agents.
    for source in sources:
        if hasattr(source, "sync_agents"):
            source.sync_agents(conn)
        if hasattr(source, "sync_titles"):
            source.sync_titles(conn)
        if hasattr(source, "reconcile"):
            source.reconcile(conn)

    # Extension hooks: satellite tables written here stay out of the returned stats
    # (/api/rescan sums every entry's turn counters). A failing hook never breaks a scan.
    for ext in extensions:
        if ext.get("on_scan"):
            try:
                ext["on_scan"](conn)
                conn.commit()  # reconcile below commits only when sources changed
            except Exception as e:
                conn.rollback()
                print(f"whytokens: extension {ext.get('name')} on_scan failed: {e}", file=sys.stderr)

    if changed:
        scanner.reconcile_session_totals(conn)
        scanner.reconcile_session_models(conn)

    if verbose:
        print("\nScan complete:")
        for name, s in stats.items():
            print(f"  [{name}] new={s['new']} updated={s['updated']} "
                  f"skipped={s['skipped']} turns={s['turns']} sessions={s['sessions']}")

    conn.close()
    return stats
