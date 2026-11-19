import unittest

from rj import pricing, schema


class Prices(unittest.TestCase):
    def setUp(self):
        self.prices = schema.load_prices()

    def test_one_entry_per_matrix_model(self):
        models = {r["model"] for r in schema.load_matrix("matrix.yaml")}
        self.assertEqual(models, set(self.prices["tokenrouter"]))

    def test_est_session_usd_recomputes(self):
        for model, entry in self.prices["tokenrouter"].items():
            got = pricing.est_session_usd(pricing.price_from_yaml(entry))
            self.assertAlmostEqual(got, entry["est_session_usd"], delta=0.01, msg=model)

    def test_core_estimates_are_the_plans(self):
        est = {m: e["est_session_usd"] for m, e in self.prices["tokenrouter"].items()}
        self.assertEqual((est["claude-haiku-4.5"], est["kimi-k2.7-code"], est["gpt-5.4-mini"], est["qwen3.7-plus"]),
                         (1.71, 2.01, 1.33, 0.86))
        self.assertEqual((est["claude-sonnet-5.5"], est["claude-sonnet-4.6"], est["gpt-5.4"], est["qwen3.7-max"],
                          est["deepseek-v4-pro"]), (2.91, 5.13, 4.43, 2.63, 1.65))

    def test_ratio_rule(self):
        p = pricing.price_from_ratios({"model_ratio": 0.5, "completion_ratio": 5, "cache_ratio": 0.1,
                                       "create_cache_ratio": 1.25})
        self.assertEqual((p.input, p.cache_read, p.cache_write, p.output), (1.0, 0.1, 1.25, 5.0))
        q = pricing.price_from_ratios({"model_ratio": 0.375, "completion_ratio": 6, "cache_ratio": None,
                                       "create_cache_ratio": None})
        self.assertEqual((q.cache_read, q.cache_write), (0.75, 0.75), "absent ratios count as 1")

    def test_turn_cost_and_the_two_readings_of_input_tokens(self):
        p = pricing.price_from_yaml(self.prices["tokenrouter"]["claude-haiku-4.5"])
        usage = {"input_tokens": 100_000, "cache_read_tokens": 70_000, "cache_write_tokens": 10_000, "output_tokens": 5_000}
        dear = pricing.turn_cost_usd(usage, p)                                # input_tokens excludes the cache
        self.assertAlmostEqual(dear, (100_000 * 1.0 + 70_000 * 0.1 + 10_000 * 1.25 + 5_000 * 5.0) / 1e6)
        cheap = pricing.turn_cost_usd(usage, p, input_includes_cache=True)   # input_tokens includes it
        self.assertAlmostEqual(cheap, (20_000 * 1.0 + 70_000 * 0.1 + 10_000 * 1.25 + 5_000 * 5.0) / 1e6)
        self.assertGreater(dear, cheap)
        self.assertIsNone(pricing.turn_cost_usd(None, p))


if __name__ == "__main__":
    unittest.main()
