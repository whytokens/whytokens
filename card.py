"""card.py - `whytokens card --out FILE`: one static, self-contained, aggregate-only file (.html or .svg)
a person can forward without exposing projects, paths, commands or prompts.

Every number comes from an existing aggregation (api.get_waste, api.get_changes, fixes._session_totals),
so pricing stays in pricing.py. The model built by `build` holds only numbers, well-formed dates, cause
labels and tier words; the only DB-derived free text that can reach it is the user's own project display
tag, and only with ``include_projects`` (explicit opt-in)."""

import sqlite3
import textwrap
import time
from html import escape
from pathlib import Path
from urllib.parse import quote

import api
import budget
import fixes
import provenance

TOP = 3
# only a well-formed leading date may reach the card: a malformed timestamp is free text from a log
_DATE = "timestamp GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"


def _totals(db_path, tz):
    day = api._day_expr(tz)
    conn = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        usd = sum(fixes._session_totals(conn).values())
        tokens, first, last = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0) + COALESCE(cache_read_tokens,0)"
            " + COALESCE(cache_creation_tokens,0)), 0), MIN(CASE WHEN {d} THEN {day} END),"
            " MAX(CASE WHEN {d} THEN {day} END) FROM turns".format(d=_DATE, day=day)).fetchone()
    finally:
        conn.close()
    return usd, tokens, first or "", last or ""


def build(db_path, include_projects=False, tz="local", wait=30):
    """The totals, causes and verdict come from separate queries; a scan committing between them would put
    numbers from two database states on one card. SQLite's data_version changes on a connection when any
    other connection commits, so build, check nothing committed meanwhile, and retry if something did.
    No lock is taken: a dashboard rescan is never refused because a card is being made."""
    watch = sqlite3.connect(f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    try:
        for attempt in range(max(1, int(wait))):
            before = watch.execute("PRAGMA data_version").fetchone()[0]
            m = _build(db_path, include_projects, tz)
            if watch.execute("PRAGMA data_version").fetchone()[0] == before:
                return m
            time.sleep(1)
    finally:
        watch.close()
    raise ValueError("the database kept changing while the card was built (a scan is running); try again when it finishes")


def _build(db_path, include_projects, tz):
    usd, tokens, first, last = _totals(db_path, tz)
    w = api.get_waste(db_path)
    causes = [{"label": fixes.CAUSE_LABELS.get(r["cause"], r["cause"]), "usd": r["usd"], "sessions": r["sessions"],
               "tier": r["tier"],
               "attribution": ("observed and inferred" if r["observed_usd"] and r["inferred_usd"]
                               else provenance.attribution(r["tier"]))}
              for r in w["rollup"] if r["in_total"]][:TOP]
    done = [c for c in api.get_changes(db_path)["changes"] if c["state"] in ("reduced", "no_change", "increased")]
    change = None
    if done:  # a reduction wins; otherwise a completed flat/up verdict, so it is never "no change yet"
        c = max(done, key=lambda c: (c["state"] == "reduced", c.get("saving_usd") or 0))
        change = {"cause": c["cause_label"], "before": c["before"], "after": c["after"], "state": c["state"]}
        tag = budget.load_tags(db_path).get(c["fix"]["project"]) if include_projects else None
        if tag:  # the user's own display tag, never the internal project name
            change["project"] = tag
    return {"period": (first, last), "calendar": "UTC" if tz == "utc" else "local time", "usd": usd, "tokens": tokens, "basis": w["basis"]["label"], "causes": causes,
            "change": change, "min_sessions": fixes.MIN_SESSIONS, "caveat": fixes.CAVEAT}


def _lines(m):
    """The card as (kind, text) rows: one source for the HTML and SVG renderings."""
    a, b = m["period"]
    rows = [("title", "AI coding usage"), ("sub", f"{a} to {b} ({m['calendar']})" if a else "period unavailable"),
            ("big", f"${m['usd']:,.2f}"),
            ("sub", f"{m['tokens']:,} tokens. API-equivalent list-rate dollars, not money spent.")]
    rows.append(("head", "Top causes of overhead"))
    for c in m["causes"]:
        rows.append(("row", f"{c['label']}: ${c['usd']:,.2f} in {c['sessions']:,} sessions ({c['tier']}, {c['attribution']})"))
    if not m["causes"]:
        rows.append(("row", "No material overhead found."))
    c = m["change"]
    rows.append(("head", "Latest change check" if c and c["state"] != "reduced" else "Verified change"))
    if c:
        tag = f" ({c['project']})" if c.get("project") else ""
        verdict = {"reduced": "", "no_change": " No material change.", "increased": " It went up."}[c["state"]]
        rows.append(("row", f"{c['cause']}{tag}: share of spend {c['before']['pct'] * 100:.1f}% over {c['before']['sessions']} sessions "
                            f"before, {c['after']['pct'] * 100:.1f}% over {c['after']['sessions']} after.{verdict}"))
    else:
        rows.append(("row", "No verified change yet."))
    rows.append(("note", f"Overhead amounts are {m['basis']}. {m['caveat']}"))
    return rows


def render_html(m):
    body = "\n".join(f'<p class="{k}">{escape(t)}</p>' for k, t in _lines(m))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>whytokens usage card</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mute:#5c6470;--line:#d9dde3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161a;--fg:#ececec;--mute:#9aa3af;--line:#2d333b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:34rem;margin:0 auto;padding:1.5rem 16px}}
p{{margin:.3rem 0}}.title{{font-weight:600;font-size:1.1rem}}.big{{font-size:2.6rem;font-weight:700;margin:.6rem 0 0}}
.sub,.note{{color:var(--mute)}}.note{{font-size:.85rem;margin-top:1.5rem;border-top:1px solid var(--line);padding-top:.8rem}}
.head{{margin-top:1.4rem;font-weight:600;border-bottom:1px solid var(--line);padding-bottom:.2rem}}
</style></head><body><main>
{body}
</main></body></html>
"""


def render_svg(m):
    size = {"title": 20, "sub": 13, "big": 40, "head": 15, "row": 13, "note": 11}
    y, out = 0, []
    for k, t in _lines(m):
        weight = ' font-weight="700"' if k in ("title", "big", "head") else ""
        cls = "m" if k in ("sub", "note") else "f"
        # shortcut: ~0.55 em per glyph estimate in an 820-unit text width; upgrade if a font breaks it
        for i, part in enumerate(textwrap.wrap(t, int(820 / (size[k] * 0.55))) or [""]):
            y += (38 if k == "head" else int(size[k] * 1.9)) if i == 0 else int(size[k] * 1.5)
            out.append(f'<text x="20" y="{y}" font-size="{size[k]}" class="{cls}"{weight}>{escape(part)}</text>')
    h = y + 24
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="860" height="{h}" viewBox="0 0 860 {h}" font-family="system-ui,sans-serif">'
            '<style>.f{fill:#1a1a1a}.m{fill:#5c6470}rect{fill:#fff}'
            '@media (prefers-color-scheme:dark){.f{fill:#ececec}.m{fill:#9aa3af}rect{fill:#14161a}}</style>'
            f'<rect width="860" height="{h}"/>' + "".join(out) + "</svg>\n")


def write(db_path, out, include_projects=False, tz="local"):
    out = Path(out)
    if out.suffix.lower() not in (".html", ".svg"):
        raise ValueError("--out must end in .html or .svg")
    if out.exists() and out.samefile(db_path):
        raise ValueError("--out is the usage database; refusing to overwrite it")
    m = build(db_path, include_projects, tz)
    out.write_text((render_svg if out.suffix.lower() == ".svg" else render_html)(m), encoding="utf-8")
    return m
