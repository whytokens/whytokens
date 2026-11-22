"""Build the clickable demo: ONE self-contained HTML file, from the synthetic corpus only.

    python scripts/build_demo.py OUT.html

Runs scripts/demo_corpus.py into a temp dir, scans it into a temp DB, records one change, serves the
real read-API in-process and snapshots its answers. The page inlines the dashboard (CSS, Chart.js and
the ES modules bundled into one script, because browsers refuse module imports from file://) and
replaces fetch() with those snapshots. Nothing of the owner's DB, projects or transcripts is read.
Limits: the date window is relative to the build day (rebuild for a fresh one); filters that ask the API a new question get the default snapshot; the demo is read-only.
"""
import json
import re
import sys
import tempfile
import threading
import urllib.request
from datetime import date, timedelta
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

STATIC = ROOT / "static"
CHANGE_DAYS_AGO = 6
# Known gap: a fixed habit drops out of the ranking, so the receipt names another cause than this change.
CHANGE = ("demo/docs", "resident_output")
BANNER = ('<div id="demo-banner" role="note">Demo on synthetic data: no real sessions, read-only. '
          'Open the receipt, then a flagged turn, then Changes.</div>')
# fixed to the bottom: the page body is a rail + content layout, a flow element would become a column
BANNER_CSS = ("#demo-banner{position:fixed;bottom:0;left:0;right:0;z-index:50;padding:6px 16px;text-align:center;"
              "font-size:13px;background:var(--accent);color:var(--card)}body{padding-bottom:32px}")
SHIM = """<script>
(function () {
  var DATA = __DATA__;
  function key(u) { var a = new URL(u, 'http://demo.invalid/'), q = Array.from(a.searchParams).map(function (p) { return p.join('='); }).sort().join('&');
    return a.pathname + (q ? '?' + q : ''); }
  window.fetch = function (u, opts) {
    var k = key(u), method = (opts && opts.method) || 'GET';
    if (method !== 'GET') return Promise.resolve(new Response(JSON.stringify({error: 'This demo is read-only'}), {status: 403}));
    var hit = DATA[k] !== undefined ? DATA[k] : DATA[k.split('?')[0]];
    if (hit === undefined) { console.warn('demo: no snapshot for', k); return Promise.resolve(new Response('{"error":"not in demo"}', {status: 404})); }
    return Promise.resolve(new Response(JSON.stringify(hit), {status: 200, headers: {'Content-Type': 'application/json'}}));
  };
})();
</script>"""

IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'\./(\w+)\.js';?", re.S)


def bundle_modules():
    """app.js and everything it imports as one script: each module an IIFE, imports destructured."""
    mods, order = {}, []

    def load(name):
        if name in mods:
            return
        mods[name] = (STATIC / "modules" / f"{name}.js").read_text(encoding="utf-8")
        for m in IMPORT.finditer(mods[name]):
            load(m.group(2))
        order.append(name)
    load("app")
    # `export let` names are reassigned by their module; importers read them off the module object.
    live = {m.group(1): n for n, s in mods.items() for m in re.finditer(r"^export let (\w+)", s, re.M)}
    out = []
    for n in order:
        src, names, own = mods[n], [], []

        def take(m):
            keep = []
            for part in (p.strip() for p in m.group(1).split(",") if p.strip()):
                if live.get(part) == m.group(2):
                    own.append((part, m.group(2)))
                else:
                    keep.append(part.replace(" as ", ": "))
            return f"const {{{', '.join(keep)}}} = __m_{m.group(2)};" if keep else ""
        src = IMPORT.sub(take, src)
        for m in re.finditer(r"^export (?:async )?(?:function|const|let|class) (\w+)", src, re.M):
            names.append(m.group(1))
        for m in re.finditer(r"^export \{([^}]*)\};?", src, re.M):
            names += [x.strip() for x in m.group(1).split(",") if x.strip()]
        src = re.sub(r"^export \{[^}]*\};?", "", src, flags=re.M)
        src = re.sub(r"^export (?=(?:async )?(?:function|const|let|class) )", "", src, flags=re.M)
        for nm, owner in own:
            src = re.sub(rf"(?<![\w.'\"]){nm}\b(?!\s*:)", f"__m_{owner}.{nm}", src)
        getters = ", ".join(f"get {x}() {{ return {x}; }}" for x in names)
        out.append(f"const __m_{n} = (() => {{\n{src}\nreturn {{{getters}}};\n}})();")
    return "\n".join(out)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never via an ambient proxy
REQUIRED = {"/api/data?tz=local", "/api/data?tz=utc", "/api/waste", "/api/changes", "/api/status"}


def scrub(o):
    """/api/status names the folders it scans (the owner's ~/.codex, the temp dir): say what they are instead."""
    if isinstance(o, dict):
        return {k: "(synthetic corpus)" if k == "path" and isinstance(v, str) else scrub(v) for k, v in o.items()}
    return [scrub(v) for v in o] if isinstance(o, list) else o


def snapshot(base, sessions, only_sessions=False):
    """GET every URL the demo journey needs from the in-process API, keyed by path?sorted-query."""
    paths = ["/api/data?tz=local", "/api/data?tz=utc", "/api/waste", "/api/shapes", "/api/fixes", "/api/changes",
             "/api/provenance", "/api/version", "/api/pricing", "/api/pricing/fallbacks", "/api/status",
             "/api/budget?tz=local", "/api/budget?tz=utc", "/api/extensions", "/api/sessions", "/api/daily"]
    if only_sessions:
        paths = []
    for sid in sessions:
        paths += [f"/api/session?session_id={sid}", f"/api/session-breakdown?session_id={sid}",
                  f"/api/waste?session_id={sid}"]
    data = {}
    for p in paths:
        try:
            with OPENER.open(base + p, timeout=60) as r:
                body = json.load(r)
        except Exception as e:
            if p in REQUIRED:
                raise SystemExit(f"demo build: required snapshot {p} failed: {e}")
            print(f"skipped {p}: {e}", file=sys.stderr)  # an endpoint the demo does not need must not stop the build
            continue
        path, _, q = p.partition("?")
        data[path + ("?" + "&".join(sorted(q.split("&"))) if q else "")] = scrub(body)
    if only_sessions:
        return data
    st = data["/api/status"]
    if st["scan"]["file_errors"] or st["scan"]["error"]:
        raise SystemExit("demo build: the scan reported errors; they would carry local paths")
    st["roots"] = [{"source": "claude", "path": "(synthetic corpus)", "exists": True}]  # not this machine's real roots
    return data


def build(out):
    import api
    import demo_corpus
    import scanner
    import sources
    import sources.codex as codex_mod
    from sources.claude import ClaudeSource

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # enough sessions for a verdict (10 per side) and one project that really stops its habit
        demo_corpus.main(tmp / "p", sessions=240, fixed=(CHANGE[0], CHANGE_DAYS_AGO))
        db = tmp / "demo.db"
        (tmp / "c").mkdir()
        sources.scan_all(db_path=db, sources=[ClaudeSource(projects_dirs=[tmp / "p"])], extensions=[], verbose=False)
        patches = [mock.patch.object(api, "DB_PATH", db), mock.patch.object(scanner, "DEFAULT_PROJECTS_DIRS", [tmp / "p"]),
                   mock.patch.object(codex_mod, "CODEX_PATH", tmp / "c"),
                   mock.patch.object(api.DashboardHandler, "token", "demo"),
                   mock.patch.object(api.DashboardHandler, "allow_any_host", False)]
        for p in patches:
            p.start()
        server = HTTPServer(("127.0.0.1", 0), api.DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            api.post_fix({"project": CHANGE[0], "cause": CHANGE[1], "note": "Pipe long command output through tail",
                          "date": (date.today() - timedelta(days=CHANGE_DAYS_AGO)).isoformat()}, db_path=db)
            sessions = sorted({f["session_id"] for f in api.get_waste(db_path=db)["findings"]})
            base = f"http://127.0.0.1:{server.server_address[1]}"
            data = snapshot(base, sessions)
            listed = [x["full_session_id"] for x in data["/api/data?tz=local"]["sessions_all"]]
            data.update(snapshot(base, [x for x in listed if x not in sessions], only_sessions=True))  # every row the Sessions tab lists
        finally:
            server.shutdown()
            for p in patches:
                p.stop()

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8") + BANNER_CSS
    chart = (STATIC / "vendor" / "chart.umd.min.js").read_text(encoding="utf-8")
    safe = lambda s: s.replace("</script", "<\\/script")
    html = html.replace('<link rel="stylesheet" href="/static/styles.css">', "<style>" + css + "</style>", 1)
    html = html.replace('<script src="/static/vendor/chart.umd.min.js"></script>', "<script>" + safe(chart) + "</script>", 1)
    html = html.replace('<script type="module" src="/static/modules/app.js"></script>',
                        SHIM.replace("__DATA__", safe(json.dumps(data))) + '<script type="module">' + safe(bundle_modules()) + "</script>", 1)
    html = html.replace('<body class="booting">', '<body class="booting">' + BANNER, 1)
    for mine in (str(tmp), str(Path.home())):  # the build itself refuses to write a page that names this machine
        if any(form in html for form in (mine, mine.replace("\\", "/"), json.dumps(mine)[1:-1])):
            raise SystemExit("demo build: the page names a local path")
    Path(out).write_text(html, encoding="utf-8")


if __name__ == "__main__":
    build(sys.argv[1])
