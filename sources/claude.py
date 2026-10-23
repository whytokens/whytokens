"""sources/claude.py - Claude Code as an ingestion source.

A deliberately thin wrapper over the vendored ``scanner.py``: discovery and parsing
are delegated to the existing functions unchanged, so the scanner stays diff-aligned
with upstream and its hard-won parser is reused verbatim. The only added behaviour is
tagging each turn ``agent='claude'`` (harmless even though the DB column already
defaults to 'claude') so the multi-source engine and session rollups attribute the
source explicitly.
"""

import glob
import json
from pathlib import Path

import scanner

NAME = "claude"


def _attribution_agent(path):
    """First ``attributionAgent`` name in a subagent's own transcript, else None.

    Every assistant record inside a subagent's jsonl carries the same
    human-readable name the parent's tool_result may have omitted, so it is the
    fallback when the ``.meta.json`` sidecar is missing. Cheap line prefilter,
    and the read stops at the first hit — one name is all the sweep needs.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "attributionAgent" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = record.get("attributionAgent")
                if name:
                    return name
    except Exception as e:
        print(f"  Warning: error reading {path}: {e}")
    return None


class ClaudeSource:
    name = NAME

    def __init__(self, projects_dirs=None, blob_dir=None):
        # Default to the scanner's own project dirs (~/.claude/projects + the Xcode
        # location); tests/CLI may override with a single dir.
        self.projects_dirs = projects_dirs
        # Desktop-app IndexedDB cache holding Projects thread titles. Like
        # the sidecar dirs: only the default (no dirs passed) reads the real one, so a
        # test passing its own projects dir never touches %APPDATA%.
        if blob_dir is None and not projects_dirs:
            import config
            blob_dir = config.desktop_cache_dir()
        self.blob_dir = blob_dir

    def _dirs(self):
        if self.projects_dirs:
            return [Path(d) for d in self.projects_dirs]
        # Default roots (~/.claude/projects + Xcode), plus any extra eval roots from
        # WHYTOKENS_EXTRA_PROJECTS_DIRS — see config.claude_projects_dirs.
        import config
        return config.claude_projects_dirs(scanner.DEFAULT_PROJECTS_DIRS)

    def discover_files(self):
        files = []
        for d in self._dirs():
            d = Path(d)
            if d.exists():
                files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
        files.sort()
        return files

    def parse_file(self, filepath, start_line=0):
        agents = {}
        metas, turns, line_count = scanner.parse_jsonl_file(
            filepath, start_line=start_line, agents_out=agents)
        for t in turns:
            t["agent"] = NAME
        for m in metas:
            m["agent"] = NAME
        # 4th element: subagent dispatch rows for scan_all to upsert (Claude-only).
        return metas, turns, line_count, list(agents.values())

    def sync_agents(self, conn):
        """Fill in ``agents.agent_type`` from each subagent's own files.

        Two passes, both keyed off the agent id in the filename: the
        ``.meta.json`` sidecar first, then ``attributionAgent`` inside the
        subagent's own jsonl for any agent the sidecar did not name.

        Claude Code writes every dispatched subagent to
        ``<project>/<session-id>/subagents/agent-<id>.jsonl`` with a sibling
        ``agent-<id>.meta.json`` holding ``agentType`` (plus description/model).
        The scanner's other source of that name is the parent's tool_result record,
        which frequently omits ``agentType`` — on a real corpus roughly half the
        ``agents`` rows end up with a NULL type, which collapses every one of those
        subagents into "main thread" in any per-agent breakdown.

        This sweep is deliberately NOT gated by a schema_meta marker: it is cheap
        (only meta files whose agent is unknown or still untyped are opened) and
        self-healing, so a corpus that grows new agent types between scans converges
        without a one-shot backfill to remember. Runs after the walk so subagent
        files discovered by the same scan are already in ``agents``.
        """
        known = {row[0] for row in conn.execute(
            "SELECT agent_id FROM agents WHERE agent_type IS NOT NULL")}
        rows = []
        named = set()  # ids this sweep has already named, across both passes
        for d in self._dirs():
            d = Path(d)
            if not d.exists():
                continue
            for meta_path in d.glob("**/subagents/agent-*.meta.json"):
                agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                if agent_id in known:
                    continue
                try:
                    with open(meta_path, encoding="utf-8", errors="replace") as f:
                        meta = json.load(f)
                except Exception as e:
                    print(f"  Warning: error reading {meta_path}: {e}")
                    continue
                agent_type = meta.get("agentType")
                if not agent_type:
                    continue
                named.add(agent_id)
                rows.append({
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    # <project>/<session-id>/subagents/<file> -> the session that
                    # dispatched it. Only used when the tool_result record that
                    # normally supplies it was never seen; COALESCE keeps a real
                    # value from being overwritten.
                    "dispatched_in_session": meta_path.parent.parent.name,
                })

            # Fallback for a subagent with no .meta.json sidecar — a dispatch mode
            # that never writes one, or a transcript tree that lost it. The agent's
            # own jsonl carries `attributionAgent`, the same name. Upstream claude-usage
            # reads this at parse time behind a one-shot backfill; here it is one
            # more pass of the existing sweep, which stays self-healing and keeps
            # the vendored scanner untouched.
            for jsonl_path in d.glob("**/subagents/agent-*.jsonl"):
                agent_id = jsonl_path.name[len("agent-"):-len(".jsonl")]
                if agent_id in known or agent_id in named:
                    continue
                agent_type = _attribution_agent(jsonl_path)
                if not agent_type:
                    continue
                named.add(agent_id)
                rows.append({
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "dispatched_in_session": jsonl_path.parent.parent.name,
                })
        if rows:
            scanner.upsert_agents(conn, rows)
            conn.commit()
        return len(rows)

    def sync_titles(self, conn):
        """Title Claude Projects thread sessions (see project_threads). Runs after the
        walk on every scan: a thread's first transcript line lands before the
        coordinator's cached spawn is readable, and the cache is a rolling window,
        so capture whenever it is there."""
        import project_threads
        try:
            return project_threads.apply_thread_titles(conn, self.discover_files(), self.blob_dir)
        except Exception as e:  # a title pass must never fail the scan
            print(f"  Warning: thread titles skipped: {e}")
            return 0

    def backfill_topics(self, conn):
        """One-time topic backfill (see scanner.backfill_topics_once). Codex has no
        title records, so only ClaudeSource implements this hook; scan_all calls it
        via hasattr, leaving CodexSource untouched (its sessions keep topic NULL).

        dirs_found gates the "done" marker: if no configured projects dir actually
        exists (missing/unreadable), the backfill is skipped AND the marker stays
        unset, so a later scan retries once the directory appears.
        """
        dirs_found = any(Path(d).exists() for d in self._dirs())
        return scanner.backfill_topics_once(conn, self.discover_files(), dirs_found=dirs_found)

    def backfill_advisor_turns(self, conn):
        """One-time forced re-parse so an upgraded DB gains its advisor turns.

        Unlike topics/effort this cannot be an UPDATE: advisor turns are rows that
        did not exist before, and the incremental walk skips files whose mtime is
        unchanged — so without this an existing DB keeps its pre-fix undercount
        forever. See scanner.force_reparse_once for why repeating it is harmless.
        """
        if not any(Path(d).exists() for d in self._dirs()):
            return 0  # no transcript dir: leave the marker unset and retry later
        cleared = scanner.force_reparse_once(
            conn, "advisor_reparse_done_claude", NAME)
        # Same one-shot re-read so rows frozen mid-stream (partial tallies kept by
        # the old INSERT OR IGNORE) meet their final record under the new upsert.
        cleared += scanner.force_reparse_once(
            conn, "final_tallies_reparse_done_claude", NAME,
            why="to correct turns stored mid-stream")
        # v2: v1 keyed summaries without the session id and could keep a stale :est
        # beside a real advisor row. Drop every synthetic estimate row once so the
        # re-parse regenerates them under the current keys.
        if scanner._meta_get(conn, "subagent_estimates_v2_reparse_done_claude") != "1":
            conn.execute("DELETE FROM turns WHERE message_id LIKE 'agent_summary:%' "
                         "OR message_id LIKE 'advisor:%:est'")
        return cleared + scanner.force_reparse_once(
            conn, "subagent_estimates_v2_reparse_done_claude", NAME,
            why="to add subagent output/advisor/AgentSummary estimates")

    def backfill_effort(self, conn):
        """One-time turns.effort backfill (see scanner.backfill_effort_once).

        Cheap line prefilter like _backfill_topics: only records that mention
        "effort" are JSON-parsed, which is ~0.1% of lines. A tool_use input can
        also carry an "effort" key, but only the record's *top-level* effort is
        read, so those parse and fall through.
        """
        if not any(Path(d).exists() for d in self._dirs()):
            return 0  # no transcript dir: leave the marker unset and retry later

        def pairs():
            for filepath in self.discover_files():
                try:
                    with open(filepath, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if '"effort"' not in line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            message = record.get("message")
                            if not isinstance(message, dict):
                                continue
                            yield message.get("id"), record.get("effort")
                except Exception as e:
                    print(f"  Warning: error reading {filepath}: {e}")

        return scanner.backfill_effort_once(conn, "effort_backfill_done_claude", pairs)

    def backfill_skill(self, conn):
        """One-time turns.skill backfill (see scanner.backfill_column_once).

        Same shape as backfill_effort: rows ingested before the column existed can
        never be fixed by a rescan (INSERT OR IGNORE leaves them alone), so they are
        rewritten once. Cheap line prefilter — only records mentioning
        "attributionSkill" are JSON-parsed — and the shared guard skips the corpus
        read entirely when there is nothing to fill.
        """
        if not any(Path(d).exists() for d in self._dirs()):
            return 0  # no transcript dir: leave the marker unset and retry later

        def pairs():
            for filepath in self.discover_files():
                try:
                    with open(filepath, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if '"attributionSkill"' not in line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            message = record.get("message")
                            if not isinstance(message, dict):
                                continue
                            yield message.get("id"), record.get("attributionSkill")
                except Exception as e:
                    print(f"  Warning: error reading {filepath}: {e}")

        filled = scanner.backfill_column_once(
            conn, "skill_backfill_done_claude", "skill", pairs, label="skill")
        # Advisor turns inherit their parent's skill at parse time, but historical
        # rows are keyed by a synthetic message_id the pairs above can never match.
        # Cheap and idempotent, so it runs on every scan rather than under a marker:
        # a scan that adds new turns can also add advisor rows needing the same fix.
        scanner.propagate_advisor_column(conn, "skill")
        conn.commit()
        return filled

    def backfill_speed(self, conn):
        """One-time turns.speed backfill (see scanner.backfill_column_once). Only
        'fast' is priced, so only lines mentioning "fast" are parsed. Advisor rows
        are keyed by a synthetic id the pairs never match, which is right: the
        parent's fast mode doesn't apply to the advisor's inference."""
        if not any(Path(d).exists() for d in self._dirs()):
            return 0  # no transcript dir: leave the marker unset and retry later

        def pairs():
            for filepath in self.discover_files():
                try:
                    with open(filepath, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if '"fast"' not in line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            message = record.get("message")
                            if not isinstance(message, dict):
                                continue
                            if (message.get("usage") or {}).get("speed") == "fast":
                                yield message.get("id"), "fast"
                except Exception as e:
                    print(f"  Warning: error reading {filepath}: {e}")

        return scanner.backfill_column_once(
            conn, "speed_backfill_done_claude", "speed", pairs, label="speed")

    MCP_COLUMNS = (("mcp_server", "attributionMcpServer"),
                   ("mcp_tool", "attributionMcpTool"),
                   ("plugin", "attributionPlugin"))

    def backfill_mcp(self, conn):
        """One-time backfill of turns.mcp_server / mcp_tool / plugin.

        Same shape as backfill_skill, but the three columns share ONE corpus read:
        the (message_id, values) rows are collected once and each column is then
        filled under its own marker. Most turns carry no MCP attribution, so the
        guard's "nothing to fill" shortcut never fires here — the read happens once
        per column marker, which the shared list makes once in total.
        """
        if not any(Path(d).exists() for d in self._dirs()):
            return 0
        rows = None

        def collect():
            nonlocal rows
            if rows is not None:
                return rows
            rows = []
            for filepath in self.discover_files():
                try:
                    with open(filepath, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if '"attributionMcp' not in line and '"attributionPlugin"' not in line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            message = record.get("message")
                            if not isinstance(message, dict):
                                continue
                            rows.append((message.get("id"),
                                         [record.get(k) for _, k in self.MCP_COLUMNS]))
                except Exception as e:
                    print(f"  Warning: error reading {filepath}: {e}")
            return rows

        filled = 0
        for i, (col, _) in enumerate(self.MCP_COLUMNS):
            filled += scanner.backfill_column_once(
                conn, f"{col}_backfill_done_claude", col,
                lambda i=i: ((mid, vals[i]) for mid, vals in collect()), label=col)
            scanner.propagate_advisor_column(conn, col)
        conn.commit()
        return filled
