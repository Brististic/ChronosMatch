"""Day 10 tests: the validation harness itself (validation/run_match_validation.py).

A validation report is only worth something if the harness can FAIL. These tests run it at a tiny size,
and then break the reference engine on purpose to prove the harness notices.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "validation", "run_match_validation.py")


def load_harness():
    spec = importlib.util.spec_from_file_location("run_match_validation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestValidationHarness(unittest.TestCase):
    def setUp(self):
        self.mod = load_harness()

    def test_harness_passes_at_small_scale(self):
        self.assertEqual(self.mod.main(["--scale", "0.001", "--out", "-"]), 0)

    def test_every_scenario_produces_trades_and_valid_orders(self):
        for title, make, size in self.mod.SCENARIOS:
            messages = make(5000)
            oracle = self.mod.Oracle()
            for kind, oid, side, tick, qty in messages:
                oracle.new(oid, side, tick, qty) if kind == "N" else oracle.cancel(oid)
            self.assertEqual(oracle.rejected, 0, title)
            self.assertGreater(oracle.trades, 100, title + ": scenario must actually trade")

    def test_scenarios_are_reproducible(self):
        for _, make, _ in self.mod.SCENARIOS:
            self.assertEqual(make(2000), make(2000))

    def test_harness_notices_a_wrong_fill_quantity(self):
        mod = self.mod

        class WrongQuantity(mod.Oracle):
            def new(self, *args):
                fills = super().new(*args)
                if fills:
                    t, m, p, q = fills[0]
                    fills[0] = (t, m, p, q + 1)
                return fills

        mod.Oracle = WrongQuantity
        result = mod.run_scenario("negative control", mod.scenario_balanced, 4000, 1.0)
        self.assertFalse(result["passed"])
        self.assertTrue(any(not ok for _, ok in result["checks"]))

    def test_harness_notices_last_in_first_out_matching(self):
        mod = self.mod

        class LastInFirstOut(mod.Oracle):
            """Matches the NEWEST order at a price first: violates time priority."""

            def new(self, oid, side, tick, qty):
                for opposite in (mod.B, mod.S):
                    for q in self.queues[opposite].values():
                        if len(q) > 1:
                            q.move_to_end(next(reversed(q)), last=False)   # newest becomes first in line
                return super().new(oid, side, tick, qty)

        mod.Oracle = LastInFirstOut
        result = mod.run_scenario("negative control", mod.scenario_hot_level, 4000, 1.0)
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
