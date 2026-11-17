"""Falsify tests/test_drilldown.py: break each piece of the drilldown on a copy of the tree
and check the test file goes red. A mutant that stays green is a hole in the tests.

    python scripts/falsify_drilldown.py        # exit 1 if any mutant is MISSED or STALE
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP, DRILL, API = "static/modules/app.js", "static/modules/drill.js", "api.py"

MUTANTS = [
    # server: the per-session endpoint
    (API, "waste.report(db_path, session_id=session_id) if", "waste.report(db_path) if"),
    (API, 'get_waste(session_id=_qs1(parse_qs(parsed.query), "session_id"))', "get_waste()"),
    (API, '"total_usd": sum(f["usd"] for f in found if f.get("in_total", True))}', '"total_usd": 0.0}'),
    # URL state
    (DRILL, "if (f.cause) params.set('cause', f.cause);", ""),
    (DRILL, "if (f.minCost) params.set('smin', String(f.minCost));", ""),
    (DRILL, "if (f.hasCacheCreation) params.set('scache', '1');", ""),
    (DRILL, "SESSION_SORTS.includes(col)", "true"),
    (DRILL, "/^\\d{4}-\\d{2}-\\d{2}$/.test(q.get('week') || '')", "true"),
    (DRILL, "if (tab && tab !== 'overview') p.set('tab', tab);", "p.set('tab', tab);"),
    # card targets
    (DRILL, "'Est. Cost': { col: 'cost', dir: 'desc' }", "'Est. Cost': { col: 'cost', dir: 'asc' }"),
    (DRILL, "'Cache Read': { col: 'cache_share', dir: 'asc' }", "'Cache Read': { col: 'cache_share', dir: 'desc' }"),
    (DRILL, "'Cache Creation': { col: 'cache_creation', dir: 'desc' }", "'Cache Creation': { col: 'cache', dir: 'desc' }"),
    (DRILL, "'Sessions': DEFAULT_SORT,", "'Sessions': { col: 'cost', dir: 'desc' },"),
    # overhead index
    (DRILL, "e.byCause[f.cause] = (e.byCause[f.cause] || 0) + f.usd;", "e.byCause[f.cause] = f.usd;"),
    (DRILL, "(cause ? e.byCause[cause] || 0 : e.total)", "e.total"),
    # Overview cards and strip
    (APP, '<a class="stat-card" data-drill', '<div class="stat-card" data-drill'),
    (APP, "href: drillHref(globalParams(), { tab: 'waste' }) },", "href: '?' },"),
    (APP, "v.causes.filter(c => c.in_total).slice(0, 3)", "v.causes.filter(c => c.in_total).slice(0, 4)"),
    (APP, "const v = groupWaste(p.findings, lastScopeSessions);", "const v = groupWaste(p.findings, lastFilteredSessions);"),
    (APP, "lower bound, not savings", "savings"),
    (APP, "${fmtPct(cost ? v.total / cost : 0)} of cost at list rate · lower bound", "${fmtPct(cost ? v.total / cost : 0)} of cost"),
    # Sessions list: cause filter and sorts
    (APP, "if (sessionFilters.cause && activeTab === 'sessions' && idx && !(overheadOf(idx, s.full_session_id, sessionFilters.cause) > 0)) return false;", ""),
    (APP, "if (sessionFilters.cause && activeTab === 'sessions' && idx &&", "if (sessionFilters.cause && activeTab === 'sessions' &&"),
    (APP, "av = overheadOf(idx, a.full_session_id, sessionFilters.cause); bv = overheadOf(idx, b.full_session_id, sessionFilters.cause);",
     "av = overheadOf(idx, a.full_session_id); bv = overheadOf(idx, b.full_session_id);"),
    (APP, "av = cacheShare(a); bv = cacheShare(b);", "av = a.cache_read; bv = b.cache_read;"),
    (APP, "if (tab !== 'sessions') st.filters.cause = '';", ""),
    (APP, "wasteCause = tab === 'waste' && ws.cause ? ws.cause : null;", "wasteCause = null;"),
    (APP, "{ col: 'overhead', dir: 'desc' } });", "{ col: 'cost', dir: 'desc' } });"),
    # history
    (APP, "history[push ? 'pushState' : 'replaceState']", "history.replaceState"),
    (APP, "window.addEventListener('popstate', restoreFromURL);", ""),
    (APP, "updateURL(hash, changed);", "updateURL(hash);"),
    (APP, "writeWasteState(params, wasteCause, wasteWeek)", "0"),
    (APP, "  updateURL();\n  applyFilter();\n}\n\nfunction clearSessionFilters", "  applyFilter();\n}\n\nfunction clearSessionFilters"),
    (APP, "  wasteShowAll = false;\n  updateURL();\n  await renderWasteTab();\n  // the bars", "  wasteShowAll = false;\n  await renderWasteTab();\n  // the bars"),
    (APP, '<a data-drill href="${esc(sessionsByCauseHref(c.cause))}"', '<a href="${esc(sessionsByCauseHref(c.cause))}"'),
    # Trace panel
    (APP, "  renderSessionFindings(d);\n}", "}"),
    (APP, "const fullTrace = inTrace ? '' :", "const fullTrace = false ? '' :"),
    (APP, "if (!inTrace && (s.cause === 'resident_output'", "if ((s.cause === 'resident_output'"),
    (APP, "'<p class=\"muted\">No attributed overhead found in this session.</p>'", "''"),
    (APP, "nextStepBody({ ...f, id: f.session_id }, f.fix, (label + ': ' + f.detail).slice(0, 200), true, project, true)",
     "nextStepBody({ ...f, id: f.session_id }, f.fix, (label + ': ' + f.detail).slice(0, 200), true, null, true)"),
    (APP, "traceOpenSeq++;  // a trace still loading", "//"),
    (APP, "buildFilterUI(rawData.all_models, readURLModels(rawData.all_models));", ""),
    (APP, "if (tzChanged) loadData(); else applyFilter();", "applyFilter();"),
    (APP, "sessionFilters.cause && activeTab === 'sessions' && idx &&", "sessionFilters.cause && idx &&"),
    (APP, "if (box.dataset.session !== id) {", "if (false) {"),
    (APP, "if (sessionFindings.get(id) !== req && p) return;", ""),
    ("static/index.html", 'id="where-to-act"', 'id="where-act"'),
    ("static/index.html", "setSessionSort('overhead')", "setSessionSort('cost')"),
]
TESTS = ["tests.test_drilldown", "tests.test_waste_view", "tests.test_waste_tab"]


def run(tree):
    return subprocess.run([sys.executable, "-m", "unittest", *TESTS],
                          cwd=tree, capture_output=True, text=True).returncode


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "repo"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.db", "build", "*.egg-info", ".claude", ".local-delegate"))
        if run(tree) != 0:
            sys.exit("baseline suite is red; fix that first")
        for fname, orig, mutant in MUTANTS:
            path = tree / fname
            src = path.read_text(encoding="utf-8")
            if src.count(orig) != 1:
                print(f"STALE  {fname}: pattern not found once: {orig[:60]!r}")
                failures += 1
                continue
            path.write_text(src.replace(orig, mutant), encoding="utf-8")
            caught = run(tree) != 0
            path.write_text(src, encoding="utf-8")
            print(f"{'caught' if caught else 'MISSED'} {fname}: {orig.strip()[:60]}")
            failures += not caught
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
