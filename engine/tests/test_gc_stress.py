"""Day 14 stress test: matching while cyclic GC is disabled."""

import gc
import unittest

from chronos_engine import OrderBook


B = b"B"
S = b"S"


class TestGCStress(unittest.TestCase):
    def test_continuous_matching_without_gc_collections(self):
        book = OrderBook()
        gc_was_enabled = gc.isenabled()

        # Capture collection totals before the stress workload.
        before = [item["collections"] for item in gc.get_stats()]

        try:
            gc.disable()
            self.assertFalse(gc.isenabled())

            # Each resting sell is immediately matched by a crossing buy.
            # The book should return to empty after every pair.
            order_id = 1
            iterations = 20_000

            for _ in range(iterations):
                self.assertEqual(
                    book.submit(order_id, S, 100.0, 1),
                    [],
                )
                order_id += 1

                fills = book.submit(order_id, B, 100.0, 1)
                self.assertEqual(
                    fills,
                    [(order_id, order_id - 1, 100.0, 1)],
                )
                order_id += 1

            after = [item["collections"] for item in gc.get_stats()]

            self.assertEqual(
                after,
                before,
                "GC collection counters changed during the disabled-GC workload",
            )
            self.assertEqual(len(book), 0)
            self.assertTrue(book.check_integrity())

        finally:
            # Preserve the caller's original GC state.
            if gc_was_enabled:
                gc.enable()
            else:
                gc.disable()


if __name__ == "__main__":
    unittest.main()