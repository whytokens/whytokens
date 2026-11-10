"""Configured MCP servers from Claude Code's user, project and plugin config (doctor check).

Read-only and static: nothing is launched, so tool names and schema sizes are unknown here and the
report is counts plus duplicates. Two entries are the same server when their launch spec (command +
args, or url) matches, whatever they are called. Stdlib only.
"""

import json
from pathlib import Path


def _load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _servers(block):
    return block.items() if isinstance(block, dict) else ()


def configured_servers(home, project_dir=None):
    """[(scope, name, spec_key)]. ``home`` holds ``.claude.json`` and ``.claude/plugins``."""
    home = Path(home)
    found = []
    user = _load(home / ".claude.json")
    found += [("user", n, s) for n, s in _servers(user.get("mcpServers"))]
    for proj, cfg in _servers(user.get("projects")):
        found += [(f"user-project", n, s) for n, s in _servers(cfg.get("mcpServers") if isinstance(cfg, dict) else None)]
    if project_dir:
        found += [("project", n, s) for n, s in _servers(_load(Path(project_dir) / ".mcp.json").get("mcpServers"))]
    for f in sorted((home / ".claude" / "plugins").glob("**/.mcp.json")):
        cfg = _load(f)
        # plugin files are either {"mcpServers": {...}} or the bare {name: spec} map
        found += [("plugin", n, s) for n, s in _servers(cfg.get("mcpServers", cfg))]
    return [(scope, name, _spec_key(spec)) for scope, name, spec in found]


def _spec_key(spec):
    if not isinstance(spec, dict):
        return None
    if spec.get("url"):
        return ("url", spec["url"])
    return ("cmd", spec.get("command"), tuple(map(str, spec.get("args") or ())))


def duplicates(servers):
    """Groups of 2+ entries with one launch spec: [[(scope, name), ...]]."""
    groups = {}
    for scope, name, key in servers:
        if key and key != ("cmd", None, ()):
            groups.setdefault(key, []).append((scope, name))
    # a set: stale cached plugin versions repeat one (scope, name) and are not a second install
    return [sorted(set(g)) for g in groups.values() if len(set(g)) > 1]


def format_report(servers):
    lines = [f"MCP servers configured: {len(servers)} "
             "(tool names and schema sizes are unknown without launching servers)"]
    for scope in ("user", "user-project", "project", "plugin"):
        names = sorted({n for s, n, _ in servers if s == scope})
        if names:
            lines.append(f"  {scope}: {len(names)} ({', '.join(names[:8])}{', ...' if len(names) > 8 else ''})")
    dups = duplicates(servers)
    for g in dups:
        lines.append("  duplicate install: " + " = ".join(f"{n} [{s}]" for s, n in g))
    if not dups:
        lines.append("  no duplicate servers")
    return "\n".join(lines)
