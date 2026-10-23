"""Guards for the dashboard shell: one chart-colour token map, theme + rail
storage safety, legacy ?tab= redirects, rail accessibility. String-level checks
by design (a browser is the real verifier); each one targets a rule that broke
silently before or would break silently on a refactor."""
import re
import unittest
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_MODULES = sorted((_STATIC / "modules").glob("*.js"))
CSS = (_STATIC / "styles.css").read_text(encoding="utf-8")
HTML = (_STATIC / "index.html").read_text(encoding="utf-8")
THEME_JS = (_STATIC / "modules" / "theme.js").read_text(encoding="utf-8")
NAV_JS = (_STATIC / "modules" / "nav.js").read_text(encoding="utf-8")
APP_JS = (_STATIC / "modules" / "app.js").read_text(encoding="utf-8")

# A colour literal: #rgb..#rrggbbaa (not an &#123; entity) or rgb()/rgba()/hsl()/hsla() with a number.
COLOUR_LITERAL = re.compile(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(\s*\d")


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)  # comments may say "PR #151"
    return re.sub(r"(^|\s)//[^\n]*", r"\1", src)


def colour_literals(src):
    src = strip_comments(src)
    return [m.group(0) for m in COLOUR_LITERAL.finditer(src)]


class TestNoColourLiterals(unittest.TestCase):
    def test_modules_hold_no_colour_literals(self):
        """Every chart colour comes from CSS custom properties via theme.js."""
        for path in _MODULES:
            self.assertEqual(colour_literals(path.read_text(encoding="utf-8")), [], path.name)

    def test_detector_catches_each_literal_form(self):
        """Falsify the scanner itself: each form must be caught, entities must not."""
        for bad in ("const c = '#4ade80';", "x = '#fff';", "x = 'rgba(199,78,57,0.9)';",
                    "x = 'rgb( 1,2,3)';", "x = 'hsl(10, 50%, 50%)';", "x = '#12345678';", "x = '#fff'; // trailing comment"):
            self.assertTrue(colour_literals(bad), bad)
        for ok in ("btn.innerHTML = '&#9662;';", "color: C.green", "id=\"#tools\"", "alpha(C.red, 0.9)", "// upstream PR #151", "/* #abc */ x = 1;"):
            self.assertEqual(colour_literals(ok), [], ok)


class TestTokenMap(unittest.TestCase):
    def _names(self):
        base = re.search(r"const BASE = \[(.*?)\]", THEME_JS, re.S).group(1)
        toks = re.search(r"const TOKENS = \[(.*?)\]", THEME_JS, re.S).group(1)
        cats = int(re.search(r"const CATEGORICAL = (\d+)", THEME_JS).group(1))
        q = lambda blob: re.findall(r"'(\w+)'", blob)
        return q(base) + ["tok-" + t for t in q(toks)] + [f"cat-{i}" for i in range(1, cats + 1)]

    def test_every_token_is_defined_in_light_and_both_dark_blocks(self):
        """A missing custom property makes Chart.js draw black, silently."""
        light = CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))]
        dark_media = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root:not\(\[data-theme=light\]\) \{ (--[^}]*)\}", CSS).group(1)
        dark_attr = re.search(r":root\[data-theme=dark\] \{ (--[^}]*)\}", CSS).group(1)
        for name in self._names():
            for label, block in (("light", light), ("dark media", dark_media), ("dark attr", dark_attr)):
                self.assertIn(f"--{name}:", block, f"--{name} missing in {label}")

    def test_dark_blocks_are_identical(self):
        """prefers-color-scheme and the manual dark toggle must paint the same palette."""
        dark_media = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root:not\(\[data-theme=light\]\) \{ (--[^}]*)\}", CSS).group(1)
        dark_attr = re.search(r":root\[data-theme=dark\] \{ (--[^}]*)\}", CSS).group(1)
        self.assertEqual(dark_media, dark_attr)

    def test_tokens_are_six_digit_hex(self):
        """alpha() only parses #rrggbb; anything else would skip transparency silently."""
        for name in self._names():
            for m in re.finditer(rf"--{re.escape(name)}: ([^;]+);", CSS):
                self.assertRegex(m.group(1).strip(), r"^#[0-9a-fA-F]{6}$", name)

    def test_charts_redraw_on_theme_change(self):
        self.assertIn("initTheme(() => renderTab(activeTab))", APP_JS)
        self.assertIn("function refreshTokens()", APP_JS)
        self.assertIn("refreshTokens();  // re-read CSS colours", APP_JS)
        self.assertIn("prefers-color-scheme: dark", THEME_JS)


class TestStorageSafety(unittest.TestCase):
    def test_no_bare_localstorage_outside_guards(self):
        """Every localStorage access sits inside a try block (property access can throw)."""
        for path in _MODULES:
            src = strip_comments(path.read_text(encoding="utf-8"))
            for m in re.finditer(r"localStorage", src):
                before = src[:m.start()]
                line_start = before.rfind("\n") + 1
                window = src[max(0, line_start - 200):m.start()]
                self.assertIn("try", window, f"{path.name}: unguarded localStorage near {src[line_start:m.end()]!r}")

    def test_keys(self):
        self.assertIn("'whytokens.theme'", THEME_JS)
        self.assertIn("'whytokens.rail'", THEME_JS)
        self.assertIn("'whytokens.tz'", THEME_JS)
        self.assertIn("whytokens.theme", HTML)  # pre-paint snippet uses the same key


class TestNavigation(unittest.TestCase):
    def test_legacy_ids_redirect_per_mapping(self):
        for old, tab, hash_ in (("projects", "overview", "projects"), ("tools", "overview", "tools"),
                                ("pricing", "settings", "pricing")):
            self.assertRegex(NAV_JS, rf"{old}: \{{ tab: '{tab}', hash: '{hash_}' \}}")
            self.assertNotIn(f"'{old}'", re.search(r"VALID_TABS = \[(.*?)\]", NAV_JS).group(1))

    def test_legacy_lookup_ignores_inherited_keys(self):
        # ?tab=constructor must not resolve to Object.prototype.constructor.
        self.assertIn("Object.hasOwn(LEGACY_TABS, param)", NAV_JS)

    def test_session_list_filters_do_not_skew_overview_totals(self):
        body = APP_JS[APP_JS.index("function aggregate()"):APP_JS.index("lastFilteredSessions = sortSessions")]
        # filteredSessions feeds only Sessions-screen data (list, signals); Overview totals,
        # model session counts, project tables and the cost breakdown read rangeFilteredSessions.
        leaks = re.findall(r"of filteredSessions\b|\bfilteredSessions\.length|aggregateCostBreakdown\(filteredSessions", body)
        self.assertEqual(leaks, [], "an Overview aggregate reads the session-list-filtered sessions")

    def test_tz_omitted_only_when_it_matches_the_default(self):
        self.assertIn("if (displayTZ !== (store.get(TZ_KEY)", APP_JS)

    def test_six_screens_in_rail_order(self):
        ids = re.findall(r"'(\w+)'", re.search(r"VALID_TABS = \[(.*?)\]", NAV_JS).group(1))
        self.assertEqual(ids, ["overview", "sessions", "trace", "waste", "sources", "settings"])

    def test_deep_link_hash_and_session_survive_url_rewrite(self):
        self.assertIn("(hash ? '#' + hash : '')", APP_JS)
        self.assertIn("activeTab === 'trace' && selectedSessionDetail", APP_JS)

    def test_extension_seam_does_not_depend_on_removed_panels(self):
        self.assertNotIn("panel-pricing", APP_JS + NAV_JS)
        self.assertIn("rail-spacer", NAV_JS)  # ext items go before the expand arrow

    def test_subagent_chart_only_built_while_visible(self):
        self.assertIn("document.getElementById('tools')).open) renderSubagentChart", APP_JS)


class TestRailAccessibility(unittest.TestCase):
    def test_toggle_is_a_named_button_with_aria_expanded(self):
        m = re.search(r'<button[^>]*id="rail-toggle"[^>]*>', HTML)
        self.assertIsNotNone(m)
        tag = m.group(0)
        for needle in ('aria-expanded="false"', 'aria-label="Navigation labels"', 'aria-controls="tab-bar"', "toggleRail()"):
            self.assertIn(needle, tag)
        self.assertIn("setAttribute('aria-expanded'", NAV_JS)

    def test_every_rail_item_has_name_and_tooltip(self):
        items = re.findall(r'<button class="tab-btn" id="tab-[^>]*>', HTML)
        self.assertEqual(len(items), 6)
        for tag in items:
            self.assertRegex(tag, r'aria-label="[^"]+"')
            self.assertRegex(tag, r'title="[^"]+"')

    def test_focus_ring_and_keyboard_reachable_chips(self):
        self.assertIn(":focus-visible", CSS)
        self.assertIn(".model-cb-label:focus-within", CSS)
        self.assertNotRegex(CSS, r"\.model-cb-label input \{ display: none")
        self.assertIn("th.sortable", APP_JS)  # keyboard activation wired

    def test_narrow_screens_force_icon_rail(self):
        self.assertRegex(CSS, r"@media \(max-width: 700px\)[^@]*#rail-toggle \{ display: none")


if __name__ == "__main__":
    unittest.main()
