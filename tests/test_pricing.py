"""Tests for pricing.py - the single source of truth for the price table."""

import unittest
from pathlib import Path

import pricing


class TestPricingSingleSource(unittest.TestCase):
    """The price table lives only in pricing.py. cli.py and the
    /api/pricing endpoint must both read it — no second copy anywhere."""

    def test_cli_reexports_the_same_table_object(self):
        import cli
        # cli imports from pricing; it must be the SAME object, not a copy.
        self.assertIs(cli.PRICING, pricing.PRICING)
        self.assertIs(cli.get_pricing, pricing.get_pricing)
        self.assertIs(cli.calc_cost, pricing.calc_cost)

    def test_table_has_expected_models(self):
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
            self.assertIn(model, pricing.PRICING)
            entry = pricing.PRICING[model]
            for field in ("input", "output", "cache_read", "cache_write"):
                self.assertIn(field, entry)

    def test_no_hardcoded_table_in_frontend(self):
        """pricing.js must fetch the table (via /api/pricing), not hardcode it."""
        pricing_js = (
            Path(__file__).resolve().parent.parent / "static" / "modules" / "pricing.js"
        ).read_text(encoding="utf-8")
        # DEFAULT_PRICING starts empty and is filled from the server.
        self.assertIn("DEFAULT_PRICING = {}", pricing_js)
        self.assertNotIn("output: 25.00", pricing_js)


class TestGetPricing(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(pricing.get_pricing("claude-opus-4-6")["input"], 5.00)

    def test_opus_5_priced_at_opus_rates(self):
        p = pricing.get_pricing("claude-opus-5")
        self.assertEqual(
            (p["input"], p["output"], p["cache_read"], p["cache_write"]),
            (5.00, 25.00, 0.50, 6.25),
        )

    def test_prefix_match(self):
        self.assertEqual(pricing.get_pricing("claude-sonnet-4-6-20260401")["output"], 15.00)

    def test_substring_fallback(self):
        self.assertEqual(pricing.get_pricing("new-opus-5-model")["input"], 5.00)
        self.assertEqual(pricing.get_pricing("custom-sonnet")["input"], 3.00)
        self.assertEqual(pricing.get_pricing("experimental-haiku")["input"], 1.00)

    def test_unknown_returns_none(self):
        self.assertIsNone(pricing.get_pricing("glm-5.1"))
        self.assertIsNone(pricing.get_pricing(None))
        self.assertIsNone(pricing.get_pricing(""))


class TestCodexPricing(unittest.TestCase):
    """OpenAI/Codex rates (web-verified June 2026)."""

    def test_codex_models_present(self):
        for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"):
            self.assertIn(model, pricing.PRICING)

    def test_exact_rates(self):
        self.assertEqual(pricing.get_pricing("gpt-5.5"),
                         {"input": 5.00, "output": 30.00, "cache_read": 0.50, "cache_write": 0.0})
        self.assertEqual(pricing.get_pricing("gpt-5.3-codex")["output"], 14.00)
        self.assertEqual(pricing.get_pricing("gpt-5.3-codex")["cache_read"], 0.175)

    def test_mini_prefix_does_not_collide_with_base(self):
        # gpt-5.4-mini must NOT resolve to gpt-5.4 (order-sensitive prefix match).
        self.assertEqual(pricing.get_pricing("gpt-5.4-mini")["input"], 0.75)
        self.assertEqual(pricing.get_pricing("gpt-5.4")["input"], 2.50)

    def test_date_suffix_prefix_match(self):
        self.assertEqual(pricing.get_pricing("gpt-5.3-codex-20260501")["output"], 14.00)

    def test_codex_substring_fallback(self):
        # An unseen codex variant falls back to the codex family rate.
        self.assertEqual(pricing.get_pricing("gpt-5.9-codex")["output"], 14.00)

    def test_gpt5_fallback_split_by_mini(self):
        self.assertEqual(pricing.get_pricing("gpt-5-mini")["input"], 0.75)   # mini family
        self.assertEqual(pricing.get_pricing("gpt-5-turbo")["input"], 2.50)  # gpt-5 default

    def test_non_openai_and_local_stay_unpriced(self):
        # The narrow fallback must NOT mis-price these (they cost $0, correctly).
        for m in ("gpt-4o", "gpt-4-turbo", "qwen3.6-27b-mtp", "glm-5.1"):
            self.assertIsNone(pricing.get_pricing(m), f"{m} should be unpriced")

    def test_codex_cost_uses_cached_rate(self):
        # cache_read holds the cached-input rate; cache_write is 0 (no cache create).
        cost = pricing.calc_cost("gpt-5.3-codex", 1_000_000, 1_000_000, 1_000_000, 0)
        self.assertAlmostEqual(cost, (1.75 + 14.00 + 0.175))


class TestSept2026Releases(unittest.TestCase):
    """GPT-6.1 Sol and Claude Sonnet 5.5 (released 2026-09-29/30). gpt-6.1-sol
    is not a prefix of gpt-6-sol, so without its own row it fell to the astra
    fallback: 5x input, 10x cache reads. Rates from the vendors' pricing pages,
    2026-09-30."""

    def test_gpt_6_1_sol(self):
        self.assertEqual(pricing.get_pricing("gpt-6.1-sol"),
                         {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50})
        self.assertEqual(pricing.get_pricing("gpt-6.1-sol-20260929")["cache_read"], 0.10)

    def test_sonnet_5_5_explicit_row(self):
        self.assertIn("claude-sonnet-5-5", pricing.PRICING)
        self.assertEqual(pricing.get_pricing("claude-sonnet-5-5")["input"], 2.00)
        self.assertAlmostEqual(pricing.calc_cost("claude-sonnet-5-5", 1_000_000, 1_000_000, 1_000_000, 0,
                                                 timestamp="2026-10-06T23:59:59Z"), 12.20)

    def test_sonnet_5_5_cache_read_cut(self):
        """Cache reads halved on 2026-10-07; history before it keeps $0.20."""
        self.assertEqual(pricing.get_pricing("claude-sonnet-5-5", "2026-10-06T12:00:00Z")["cache_read"], 0.20)
        self.assertEqual(pricing.get_pricing("claude-sonnet-5-5", "2026-10-07T00:00:00Z")["cache_read"], 0.10)
        self.assertEqual(pricing.get_pricing("claude-sonnet-5-5-20260930", "2026-10-08T00:00:00Z")["cache_read"], 0.10)
        self.assertEqual(pricing.get_pricing("claude-sonnet-5", "2026-10-08T00:00:00Z")["cache_read"], 0.20)


class TestHaiku55Pricing(unittest.TestCase):
    """Haiku 5.5 (2026-10-07) is 10x cheaper than Haiku 4.5. Without the explicit
    guard, any id that doesn't prefix-match the table (Bedrock/Vertex style) falls
    to the generic "haiku" branch and bills at 4.5 rates, 10x over."""

    def test_rates(self):
        p = pricing.get_pricing("claude-haiku-5-5")
        self.assertEqual((p["input"], p["output"], p["cache_read"], p["cache_write"],
                          p["cache_write_1h"]), (0.10, 0.50, 0.01, 0.125, 0.20))
        self.assertEqual(pricing.get_pricing("claude-haiku-4-5")["input"], 1.00)

    def test_non_prefix_ids(self):
        for model in ("claude-haiku-5-5-20261007", "us.anthropic.claude-haiku-5-5-v1:0",
                      "anthropic/claude-haiku-5-5"):
            self.assertEqual(pricing.get_pricing(model)["input"], 0.10, model)
        self.assertEqual(pricing.get_pricing("experimental-haiku")["input"], 1.00)

    def test_js_mirrors_the_fallback(self):
        js = (Path(__file__).resolve().parent.parent
              / "static" / "modules" / "pricing.js").read_text(encoding="utf-8")
        self.assertLess(js.index("includes('haiku-5-5')) return 'claude-haiku-5-5'"),
                        js.index("includes('haiku'))  return"))


class TestGpt6SolLunaPricing(unittest.TestCase):
    """GPT-6 Sol/Luna (released 2026-09-22). Before this they fell to the
    gpt-6 fallback and billed at astra's rate: 5x Sol, 100x Luna."""

    def test_exact_and_dated(self):
        self.assertEqual(pricing.get_pricing("gpt-6-sol"),
                         {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50})
        self.assertEqual(pricing.get_pricing("gpt-6-luna"),
                         {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125})
        self.assertEqual(pricing.get_pricing("gpt-6-sol-20260922")["input"], 2.00)
        self.assertEqual(pricing.get_pricing("gpt-6-luna-20260922")["input"], 0.10)

    def test_unknown_gpt6_still_falls_back_to_astra(self):
        self.assertEqual(pricing.get_pricing("gpt-6-nova")["input"], 10.00)


class TestGpt56Pricing(unittest.TestCase):
    """GPT-5.6 family (GA 2026-07-09): sol/terra/luna + bare id aliasing to sol."""

    def test_exact_matches(self):
        # GPT-5.6 cache writes are NOT free: 1.25x input (verified July 2026).
        self.assertEqual(pricing.get_pricing("gpt-5.6-sol"),
                         {"input": 5.00, "output": 30.00, "cache_read": 0.50, "cache_write": 6.25})
        self.assertEqual(pricing.get_pricing("gpt-5.6-terra"),
                         {"input": 2.50, "output": 15.00, "cache_read": 0.25, "cache_write": 3.125})
        self.assertEqual(pricing.get_pricing("gpt-5.6-luna"),
                         {"input": 1.00, "output": 6.00, "cache_read": 0.10, "cache_write": 1.25})

    def test_prefix_matches_with_date_suffix(self):
        self.assertEqual(pricing.get_pricing("gpt-5.6-sol-20260709")["input"], 5.00)
        self.assertEqual(pricing.get_pricing("gpt-5.6-terra-20260709")["input"], 2.50)
        self.assertEqual(pricing.get_pricing("gpt-5.6-luna-20260709")["input"], 1.00)

    def test_bare_id_aliases_to_sol_not_terra(self):
        # "gpt-5.6" must not shadow the more specific -sol/-terra/-luna keys,
        # and must resolve to Sol rates (ordering rule in pricing.py).
        self.assertEqual(pricing.get_pricing("gpt-5.6"),
                         {"input": 5.00, "output": 30.00, "cache_read": 0.50, "cache_write": 6.25})

    def test_pre_56_cache_write_stays_free(self):
        # Older OpenAI models have no cache-creation concept.
        self.assertEqual(pricing.get_pricing("gpt-5.5")["cache_write"], 0.0)
        self.assertEqual(pricing.get_pricing("gpt-5.4")["cache_write"], 0.0)
        self.assertEqual(pricing.get_pricing("gpt-5.3-codex")["cache_write"], 0.0)

    def test_56_cache_write_cost(self):
        cost = pricing.calc_cost("gpt-5.6-sol", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(cost, 6.25)


class TestMythosPricing(unittest.TestCase):
    """claude-mythos-5 shares Fable 5 pricing."""

    def test_exact_match(self):
        self.assertEqual(pricing.get_pricing("claude-mythos-5"),
                         pricing.PRICING["claude-fable-5"])

    def test_keyword_fallback(self):
        self.assertEqual(pricing.get_pricing("new-mythos-5-model"),
                         pricing.PRICING["claude-fable-5"])


class TestFable51Pricing(unittest.TestCase):
    """Fable/Mythos 5.1 price cache HITS at 0.025x input, every other model at 0.1x.

    Getting this wrong is a 4x overcharge on the token class that dominates an
    agentic session, and it is silent — the model is still "billable", still
    attributed, just priced as Fable 5.
    """

    def test_cache_read_is_a_quarter_of_fable_5(self):
        self.assertEqual(pricing.get_pricing("claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(pricing.get_pricing("claude-fable-5")["cache_read"], 1.00)

    def test_everything_but_cache_read_matches_fable_5(self):
        f5, f51 = pricing.PRICING["claude-fable-5"], pricing.PRICING["claude-fable-5-1"]
        for field in ("input", "output", "cache_write", "cache_write_1h"):
            self.assertEqual(f51[field], f5[field], field)

    def test_a_dated_variant_does_not_fall_back_to_fable_5(self):
        # The prefix scan is first-wins over insertion order: if the 5.1 keys are
        # ever moved below the 5.0 keys, "claude-fable-5" swallows this id.
        self.assertEqual(
            pricing.get_pricing("claude-fable-5-1-20260901")["cache_read"], 0.25)

    def test_mythos_51_shares_it(self):
        self.assertEqual(pricing.get_pricing("claude-mythos-5-1")["cache_read"], 0.25)

    def test_keyword_fallback_prefers_51(self):
        # An id that matches no key by prefix (vendor-prefixed, say) still must not
        # land on Fable 5's cache-read rate.
        self.assertEqual(
            pricing.get_pricing("anthropic/claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(
            pricing.get_pricing("anthropic/claude-fable-5")["cache_read"], 1.00)

    def test_js_mirrors_the_51_fallback(self):
        # DEFAULT_PRICING is server-filled, but resolveKey's keyword fallback is
        # hardcoded in JS and must mirror get_pricing()'s.
        js = (Path(__file__).resolve().parent.parent
              / "static" / "modules" / "pricing.js").read_text(encoding="utf-8")
        self.assertIn("claude-fable-5-1", js)
        self.assertLess(js.index("'claude-fable-5-1'"), js.index("'claude-fable-5'"))

    def test_js_isEstimated_covers_every_priced_gpt_family(self):
        # isEstimated() gates the cost CELL independently of whether a rate
        # resolves, so a family can be priced correctly in Python and still
        # render "n/a" in the by-model table. That is exactly what gpt-6-astra
        # did: "cost by effort level" showed $0.1165 for the same turns that
        # "cost by model" showed as n/a. Sync is otherwise enforced only by a
        # comment, so assert it.
        js = (Path(__file__).resolve().parent.parent
              / "static" / "modules" / "pricing.js").read_text(encoding="utf-8")
        body = js[js.index("export function isEstimated"):]
        body = body[:body.index("}")]
        for family, sample in [("codex", "gpt-5.3-codex"),
                               ("gpt-5", "gpt-5.6-sol"),
                               ("gpt-6", "gpt-6-astra")]:
            with self.subTest(family=family):
                # Python prices it...
                self.assertIsNotNone(pricing.get_pricing(sample),
                                     f"{sample} unpriced in pricing.py")
                # ...so the JS must also call it an estimate, or the cell is n/a.
                self.assertIn(f"'{family}'", body,
                              f"isEstimated() misses {family}: {sample} renders n/a")


class TestEffectiveDatedPricing(unittest.TestCase):
    """The tier mechanism itself, exercised against a synthetic tiered model.

    No shipped model is tiered, so the machinery is tested on a fixture.
    """

    MODEL = "test-tiered-model"
    TIERS = [
        {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.00},
        {"from": "2026-09-01", "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00},
    ]

    def setUp(self):
        pricing.PRICING[self.MODEL] = self.TIERS

    def tearDown(self):
        pricing.PRICING.pop(self.MODEL, None)

    def test_first_tier_before_cutover(self):
        p = pricing.get_pricing(self.MODEL, "2026-08-15T10:00:00Z")
        self.assertEqual(p["input"], 2.00)
        self.assertEqual(p["output"], 10.00)
        self.assertEqual(p["cache_read"], 0.20)
        self.assertEqual(p["cache_write"], 2.50)

    def test_second_tier_on_and_after_cutover(self):
        for ts in ("2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z"):
            p = pricing.get_pricing(self.MODEL, ts)
            self.assertEqual(p["input"], 3.00, ts)
            self.assertEqual(p["output"], 15.00, ts)
            self.assertEqual(p["cache_read"], 0.30, ts)
            self.assertEqual(p["cache_write"], 3.75, ts)

    def test_prefix_match_still_works_with_date_suffix(self):
        self.assertEqual(pricing.get_pricing(self.MODEL + "-20260901", "2026-08-15")["input"], 2.00)
        self.assertEqual(pricing.get_pricing(self.MODEL + "-20260901", "2026-09-15")["input"], 3.00)

    def test_timestamp_none_uses_todays_tier(self):
        # timestamp=None must resolve exactly like passing today's own date.
        from datetime import datetime
        today = datetime.now().isoformat()
        self.assertEqual(pricing.get_pricing(self.MODEL),
                         pricing.get_pricing(self.MODEL, today))

    def test_flat_models_unaffected_by_timestamp(self):
        for ts in (None, "2026-01-01", "2027-01-01"):
            self.assertEqual(pricing.get_pricing("claude-opus-4-6", ts)["input"], 5.00)

    def test_calc_cost_prices_by_turn_timestamp(self):
        self.assertAlmostEqual(pricing.calc_cost(self.MODEL, 1_000_000, 0, 0, 0, "2026-08-15"), 2.00)
        self.assertAlmostEqual(pricing.calc_cost(self.MODEL, 1_000_000, 0, 0, 0, "2026-09-01"), 3.00)


class TestSonnet5IsFlat(unittest.TestCase):
    """Sonnet 5 stays $2/$10 forever now — no date-triggered repricing."""

    def test_same_rates_before_and_after_the_cancelled_cutover(self):
        for ts in (None, "2026-08-15", "2026-09-01", "2027-06-01"):
            p = pricing.get_pricing("claude-sonnet-5", ts)
            self.assertEqual((p["input"], p["output"], p["cache_read"],
                              p["cache_write"], p["cache_write_1h"]),
                             (2.00, 10.00, 0.20, 2.50, 4.00), ts)


class TestCalcCost(unittest.TestCase):
    def test_input_output_cache(self):
        # 1M Sonnet input @ $3, 1M output @ $15
        self.assertAlmostEqual(pricing.calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0), 3.00)
        self.assertAlmostEqual(pricing.calc_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0), 15.00)
        # Opus cache read = 10% of input, cache creation = 125% of input
        self.assertAlmostEqual(pricing.calc_cost("claude-opus-4-6", 0, 0, 1_000_000, 0), 0.50)
        self.assertAlmostEqual(pricing.calc_cost("claude-opus-4-6", 0, 0, 0, 1_000_000), 6.25)

    def test_unknown_model_is_free(self):
        self.assertEqual(pricing.calc_cost("gpt-4o", 1_000_000, 500_000, 0, 0), 0.0)


class TestStaticTraversalGuard(unittest.TestCase):
    """resolve_static must keep requests inside static/ (defense in depth on top
    of the HTTP-level test)."""

    def test_normal_file_resolves(self):
        import api
        self.assertIsNotNone(api.resolve_static("index.html"))
        self.assertIsNotNone(api.resolve_static("modules/app.js"))

    def test_traversal_escapes_are_rejected(self):
        import api
        self.assertIsNone(api.resolve_static("../api.py"))
        self.assertIsNone(api.resolve_static("../../etc/passwd"))

    def test_missing_file_is_none(self):
        import api
        self.assertIsNone(api.resolve_static("does-not-exist.js"))


if __name__ == "__main__":
    unittest.main()


class TestOpus55Pricing(unittest.TestCase):
    """Opus 5.5 ($4/$20, cache hits 0.05x input) must not be priced as Opus 5.

    "claude-opus-5" is a prefix of "claude-opus-5-5", so a missing or misordered
    key bills every Opus 5.5 turn at Opus 5 rates — 25% over on input/output and
    2.5x on cache reads, silently.
    """

    def test_rates(self):
        p = pricing.get_pricing("claude-opus-5-5")
        self.assertEqual((p["input"], p["output"], p["cache_read"], p["cache_write"],
                          p["cache_write_1h"]), (4.00, 20.00, 0.20, 5.00, 8.00))
        self.assertEqual(pricing.get_pricing("claude-opus-5")["input"], 5.00)

    def test_dated_and_vendor_prefixed_ids(self):
        self.assertEqual(pricing.get_pricing("claude-opus-5-5-20260922")["cache_read"], 0.20)
        self.assertEqual(pricing.get_pricing("anthropic/claude-opus-5-5")["cache_read"], 0.20)

    def test_js_mirrors_the_fallback(self):
        js = (Path(__file__).resolve().parent.parent
              / "static" / "modules" / "pricing.js").read_text(encoding="utf-8")
        self.assertLess(js.index("includes('opus-5-5')) return"),
                        js.index("includes('opus-'))  return"))
