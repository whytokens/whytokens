# Extensions

Extensions are descriptors, not a hub. Core has no built-in knowledge of any extension.

## Loading

Extensions load **only from explicit user config**, never by discovery:

- `~/.whytokens/config.json`: `{"extensions": ["C:/path/to/ext/experiments", ...]}`
- env `WHYTOKENS_EXTENSIONS` (os.pathsep-separated dirs) is added on top (tests, one-off runs).

Each directory holds `extension.py` exporting `describe()`. `extensions.load()` imports it by file
path (name `whytokens_ext_<dirname>`) and validates the descriptor. A broken extension is skipped with
one stderr line; it never breaks core.

```python
def describe():
    return {
        "name": "experiments",                       # URL namespace + tab id prefix
        "routes": [("/api/experiments", handler)],   # handler(query: dict[str,list[str]], db_path) -> JSON-able
        "tabs": [{"name": "experiments", "label": "Experiments",
                  "static_dir": "static", "module": "tab.js"}],   # served at /ext/<name>/<file>
        "on_scan": callback_or_None,                 # callback(conn) -> int|None, after a scan, before close
    }
```

`routes` are GET only and sit behind the same `_guard()` (loopback Host, etc.) as core routes.
Extension routes are consulted after core routes, so an extension cannot shadow a core path.

## Core surface

- `GET /api/extensions` -> `{"tabs": [{name, label, module_url}]}`. `app.js` reads it before
  `readURLTab`/`wireTabs`, appends the tabs, and dispatches an extension tab by dynamic
  `import(module_url)`; the module exports `render(panelEl, ctx)`. Core tabs are untouched.
- `GET /ext/<name>/<rel>` serves files from that extension's `static_dir`, reusing
  `resolve_static`'s containment (no `..`, no escape).
- `scan_all` calls each loaded extension's `on_scan(conn)` after sources and before reconcile.
- Core keeps a generic test: a table created by an extension survives a rescan.

## Running the tests

- Core (no extensions loaded): `python -m unittest discover -s tests`
- Falsifier for the core seam: `python scripts/falsify_extensions.py`.

## Database location

`~/.whytokens/whytokens.db` (`WHYTOKENS_HOME` overrides the dir, `WHYTOKENS_DB` the file).
If that file is absent and a legacy `~/.claude/usage-profiler.db` exists, the first run copies it
with the sqlite backup API (WAL-safe), verifies equal row counts per table, and keeps the legacy
file. A failed verify deletes the new file and raises. A rescan never drops the database
(transcripts get pruned). External writers default to the same path.
