"""test_pricing_check.py - `pricing check`: review diff, aliases, tiers, offline cache,
never auto-applies. No test touches the network: fetch is injected."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import pricing
import pricing_check as pc

ROOT = Path(__file__).resolve().parent.parent
LITELLM_FIELD = pc._LITELLM


def _litellm(**overrides):
    """A LiteLLM payload equal to the shipped table (active tier), aliased by provider style."""
    raw = {"sample_spec": {"note": "not a model"}}
    for model, entry in pricing.PRICING.items():
        rates = pricing._resolve_tiers(entry)
        e = {"litellm_provider": "anthropic" if model.startswith("claude") else "openai", "mode": "chat"}
        for f, k in LITELLM_FIELD.items():
            if f in rates and rates[f]:
                e[k] = rates[f] / 1e6
        e.setdefault("input_cost_per_token", rates["input"] / 1e6)
        raw[model] = e
    for key, patch in overrides.items():
        raw[key].update(patch)
    return raw


def _openrouter(raw):
    data = []
    for key, e in raw.items():
        if key.startswith("claude-opus-4-8"):
            data.append({"id": "anthropic/claude-opus-4.8", "pricing": {
                "prompt": str(e["input_cost_per_token"]), "completion": str(e["output_cost_per_token"])}})
    return {"data": data}


class PricingCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(config, "DATA_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def _check(self, lit, orr=None, **kw):
        fetch = lambda url: lit if url == pc.LITELLM_URL else (orr or {"data": []})
        return pc.check(fetch=fetch, **kw)

    def test_identical_source_reports_no_change(self):
        lines, ok = self._check(_litellm())
        text = "\n".join(lines)
        self.assertTrue(ok)
        self.assertNotIn("CHANGED", text)
        self.assertIn("0 shipped model(s) differ", text)

    def test_changed_rate_is_reported_with_cross_check(self):
        lit = _litellm()
        lit["claude-opus-4-8"]["input_cost_per_token"] = 4e-6  # shipped 5.00
        agree = "\n".join(self._check(lit, _openrouter(lit))[0])
        self.assertIn("claude-opus-4-8.input: shipped 5 -> source 4 per M  [OpenRouter 4 agrees]", agree)
        other = _openrouter(lit)
        other["data"][0]["pricing"]["prompt"] = "0.000006"
        self.assertIn("DISAGREES", "\n".join(self._check(lit, other)[0]))
        self.assertIn("not in OpenRouter", "\n".join(self._check(lit, {"data": []})[0]))

    def test_aliases_fold_onto_shipped_keys(self):
        raw = _litellm()
        e = raw.pop("claude-opus-4-8")
        raw["vertex_ai/claude-opus-4-8@20260101"] = {**e, "litellm_provider": "vertex_ai", "input_cost_per_token": 4e-6}
        text = "\n".join(self._check(raw)[0])
        self.assertIn("claude-opus-4-8.input", text)
        self.assertNotIn("NOT IN SOURCE  claude-opus-4-8", text)
        for alias in ("anthropic/claude-opus-4.8", "us.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8-20260101"):
            self.assertEqual(pc.canonical(alias), "claude-opus-4-8")

    def test_tiered_shipped_model_compares_active_tier(self):
        lit = _litellm()
        active = pricing._resolve_tiers(pricing.PRICING["claude-sonnet-5-5"])
        # source carries the OLD pre-cut cache-read rate: must show as a change against today's tier
        lit["claude-sonnet-5-5"]["cache_read_input_token_cost"] = 0.20e-6
        text = "\n".join(self._check(lit)[0])
        self.assertIn(f"claude-sonnet-5-5.cache_read: shipped {active['cache_read']:.4g} -> source 0.2", text)
        self.assertIn("dated tiers", text)

    def test_missing_model_and_new_model(self):
        lit = _litellm()
        del lit["claude-haiku-4-5"]
        lit["claude-opus-9-9"] = {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6}
        text = "\n".join(self._check(lit)[0])
        self.assertIn("NOT IN SOURCE  claude-haiku-4-5", text)
        self.assertIn("NEW IN SOURCE  claude-opus-9-9", text)

    def test_offline_uses_cache_and_never_fetches(self):
        self._check(_litellm())  # populates the cache
        boom = mock.Mock(side_effect=AssertionError("network used"))
        lines, ok = pc.check(offline=True, fetch=boom)
        self.assertTrue(ok)
        boom.assert_not_called()
        self.assertIn("cache from", "\n".join(lines))

    def test_fetch_failure_falls_back_to_cache(self):
        self._check(_litellm())
        def down(url):
            raise OSError("no network")
        lines, ok = pc.check(fetch=down)
        self.assertTrue(ok)
        self.assertIn("fetch failed", "\n".join(lines))

    def test_no_cache_and_offline_is_a_clean_failure(self):
        lines, ok = pc.check(offline=True, fetch=mock.Mock())
        self.assertFalse(ok)
        self.assertIn("no cache", "\n".join(lines))
        self.assertIn("No LiteLLM data", "\n".join(lines))

    def test_bad_response_never_replaces_a_good_cache(self):
        self._check(_litellm())
        for bad in ([], {"note": "rate limited"}):
            lines, ok = pc.check(fetch=lambda url, bad=bad: bad)
            self.assertTrue(ok)
            self.assertIn("unexpected response shape", "\n".join(lines))
        self.assertTrue(pc.check(offline=True)[1])

    def test_corrupt_cache_is_a_clean_failure_and_openrouter_is_optional(self):
        self._check(_litellm())
        (pc.cache_dir() / "openrouter.json").write_text("{not json", encoding="utf-8")
        lines, ok = pc.check(offline=True)
        self.assertTrue(ok)
        self.assertIn("openrouter: unavailable", "\n".join(lines))
        (pc.cache_dir() / "litellm.json").write_text('{"fetched_at": "x", "data": []}', encoding="utf-8")
        self.assertIn("unexpected shape", "\n".join(pc.check(offline=True)[0]))
        (pc.cache_dir() / "litellm.json").write_text("{not json", encoding="utf-8")
        lines, ok = pc.check(offline=True)
        self.assertFalse(ok)
        self.assertIn("unreadable cache", "\n".join(lines))

    def test_non_finite_rates_are_ignored_not_equal(self):
        lit = _litellm()
        lit["claude-opus-4-8"]["input_cost_per_token"] = 1e309  # json infinity
        text = "\n".join(self._check(lit)[0])
        self.assertIn("NOT IN SOURCE  claude-opus-4-8", text)
        self.assertIsNone(pc._rate("Infinity"))
        self.assertIsNone(pc._rate(-1))

    def test_iso_dated_snapshot_folds_onto_base_model(self):
        self.assertEqual(pc.canonical("gpt-5.4-2026-03-05"), "gpt-5.4")
        lit = _litellm()
        e = lit.pop("gpt-5.4")
        lit["gpt-5.4-2026-03-05"] = {**e, "input_cost_per_token": 9e-6}
        self.assertIn("gpt-5.4.input", "\n".join(self._check(lit)[0]))

    def test_alias_collision_does_not_depend_on_json_order(self):
        base = _litellm()
        stale = {**base["claude-opus-4-8"], "input_cost_per_token": 9e-6}
        a = {"claude-opus-4-8-20250101": stale, **base}
        b = {**base, "claude-opus-4-8-20250101": stale}
        for raw in (a, b):
            self.assertNotIn("claude-opus-4-8.input", "\n".join(self._check(raw)[0]))

    def test_never_applies_to_the_table(self):
        before = copy.deepcopy(pricing.PRICING)
        sums = [hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ("pricing.py", "static/modules/pricing.js")]
        lit = _litellm()
        lit["claude-opus-4-8"]["input_cost_per_token"] = 1e-6
        self._check(lit)
        self.assertEqual(pricing.PRICING, before)
        self.assertEqual(sums, [hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
                                for f in ("pricing.py", "static/modules/pricing.js")])


if __name__ == "__main__":
    unittest.main()
