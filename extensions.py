"""extensions.py - load user-configured extensions (docs/EXTENSIONS.md).

Never discovery: only dirs named in ``<WHYTOKENS_HOME>/config.json`` ("extensions": [...])
or env WHYTOKENS_EXTENSIONS (os.pathsep-separated). A broken extension is skipped with
one stderr line; it never breaks core.
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import config

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def configured_dirs():
    dirs = []
    try:
        cfg = json.loads((config.DATA_DIR / "config.json").read_text(encoding="utf-8"))
        dirs += [str(d) for d in cfg.get("extensions", [])]
    except (OSError, ValueError, AttributeError, TypeError):  # missing, bad JSON, or "extensions" not a list
        pass
    dirs += [d for d in os.environ.get("WHYTOKENS_EXTENSIONS", "").split(os.pathsep) if d]
    return [Path(d) for d in dirs]


def _validate(d, directory):
    """Return the descriptor with ``dir`` added; raise ValueError when malformed."""
    if not isinstance(d, dict):
        raise ValueError("describe() must return a dict")
    name = d.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"bad name {name!r}")
    routes = []
    for r in d.get("routes") or []:
        path, fn = r
        if not (isinstance(path, str) and path.startswith("/api/") and callable(fn)):
            raise ValueError(f"bad route {path!r}")
        routes.append((path, fn))
    tabs = []
    for t in d.get("tabs") or []:
        if not all(isinstance(t.get(k), str) and t[k] for k in ("name", "label", "static_dir", "module")):
            raise ValueError(f"bad tab {t!r}")
        tabs.append(dict(t))
    on_scan = d.get("on_scan")
    if on_scan is not None and not callable(on_scan):
        raise ValueError("on_scan must be callable or None")
    return {"name": name, "routes": routes, "tabs": tabs, "on_scan": on_scan, "dir": Path(directory)}


def load(dirs=None):
    """Import and validate each extension dir (default: the configured ones)."""
    out, seen = [], set()
    for directory in (configured_dirs() if dirs is None else [Path(d) for d in dirs]):
        try:
            mod_name = f"whytokens_ext_{directory.name}"
            spec = importlib.util.spec_from_file_location(
                mod_name, directory / "extension.py",
                submodule_search_locations=[str(directory)])  # lets extension.py use `from . import x`
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            ext = _validate(mod.describe(), directory)
            if ext["name"] in seen:
                raise ValueError(f"duplicate name {ext['name']!r}")
            seen.add(ext["name"])
            out.append(ext)
        except Exception as e:  # ponytail: broad on purpose, a bad extension must not break core
            print(f"whytokens: skipping extension {directory}: {e}", file=sys.stderr)
    return out


def static_file(ext, rel):
    """Map /ext/<name>/<rel> to a file inside the extension's static dirs, else None."""
    for t in ext["tabs"]:
        root = (ext["dir"] / t["static_dir"]).resolve()
        cand = (root / rel).resolve()
        try:
            cand.relative_to(root)
        except ValueError:
            continue
        if cand.is_file():
            return cand
    return None
