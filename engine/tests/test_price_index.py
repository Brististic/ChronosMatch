"""Day 8 tests: exact tick prices and the contiguous price-indexed book.

A price becomes an INTEGER number of ticks (price * price_scale, default 100 -> 0.01), and the
level at that price is found by one array read. These tests check exactness, the best-price
tracking, table growth, range limits and, most importantly, agreement with a simple model.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import random
import unittest

from chronos_engine import OrderBook, bench_insert_cancel

B, S = b"B", b"S"


class TestTickPrices(unittest.TestCase):
    def test_default_scale_and_tick_size(self):
        book = OrderBook()
        self.assertEqual(book.price_scale, 100)
        self.assertAlmostEqual(book.tick_size, 0.01)

    def test_custom_scales(self):
        self.assertEqual(OrderBook(price_scale=1).price_scale, 1)
        self.assertEqual(OrderBook(price_scale=10000).price_scale, 10000)
        for bad in (0, -5, 1_000_001):
            with self.assertRaises(ValueError):
                OrderBook(price_scale=bad)

    def test_price_not_on_the_tick_grid_is_rejected(self):
        book = OrderBook()
        with self.assertRaises(ValueError):
            book.insert(1, B, 100.005, 10)
        with self.assertRaises(ValueError):
            book.submit(2, B, 100.001, 10)
        book.insert(3, B, 100.01, 10)                       # exactly one tick: fine
        self.assertEqual(len(book), 1)
        self.assertTrue(book.check_integrity())

    def test_finer_scale_accepts_finer_prices(self):
        book = OrderBook(price_scale=1000)
        book.insert(1, B, 100.005, 10)
        self.assertEqual(book.best_bid(), (100.005, 10))

    def test_whole_number_scale(self):
        book = OrderBook(price_scale=1)
        book.insert(1, S, 7.0, 3)
        with self.assertRaises(ValueError):
            book.insert(2, S, 7.5, 3)

    def test_floating_point_noise_lands_on_one_level(self):
        # 0.1 + 0.2 is 0.30000000000000004 in binary floating point. With ticks it is the same
        # price as 0.3 (this was the open risk raised on Day 2 and Day 4).
        book = OrderBook()
        book.insert(1, B, 0.1 + 0.2, 10)
        book.insert(2, B, 0.3, 20)
        self.assertEqual(book.level_count(B), 1)
        self.assertEqual(book.depth(B), [(0.3, 30, 2)])
        self.assertEqual(book.level_orders(B, 0.1 + 0.2), [(1, 10), (2, 20)])

    def test_crossing_is_exact(self):
        book = OrderBook()
        book.insert(1, S, 0.3, 10)
        fills = book.submit(2, B, 0.1 + 0.2, 10)             # same price, written the noisy way
        self.assertEqual(fills, [(2, 1, 0.3, 10)])

    def test_prices_are_reported_exactly_as_typed(self):
        book = OrderBook()
        for price in (90.1, 100.25, 0.07, 1234.56, 33.33):
            book.insert(int(price * 1000), B, price, 1)
            self.assertIn((price, 1, 1), book.depth(B, 20))

    def test_fill_price_equals_resting_price_literal(self):
        book = OrderBook()
        book.insert(1, S, 90.1, 5)
        self.assertEqual(book.submit(2, B, 91.0, 5), [(2, 1, 90.1, 5)])

    def test_tiny_and_huge_prices(self):
        book = OrderBook()
        book.insert(1, B, 0.01, 1)                           # smallest valid price: one tick
        with self.assertRaises(ValueError):
            book.insert(2, B, 0.0, 1)
        with self.assertRaises(ValueError):
            book.insert(3, B, 1e15, 1)
        self.assertEqual(book.best_bid(), (0.01, 1))


class TestBestPriceTracking(unittest.TestCase):
    def test_best_moves_down_when_best_bid_cancelled(self):
        book = OrderBook()
        for oid, price in enumerate((100.0, 100.5, 99.0, 103.0, 101.0), start=1):
            book.insert(oid, B, price, 10)
        order = []
        for oid in (4, 5, 2, 1, 3):                          # cancel in best-first order
            order.append(book.best_bid()[0])
            self.assertTrue(book.cancel(oid))
            self.assertTrue(book.check_integrity())
        self.assertEqual(order, [103.0, 101.0, 100.5, 100.0, 99.0])
        self.assertIsNone(book.best_bid())

    def test_best_moves_up_when_best_ask_cancelled(self):
        book = OrderBook()
        for oid, price in enumerate((105.0, 104.0, 110.0, 104.5), start=1):
            book.insert(oid, S, price, 1)
        seen = []
        for oid in (2, 4, 1, 3):
            seen.append(book.best_ask()[0])
            book.cancel(oid)
        self.assertEqual(seen, [104.0, 104.5, 105.0, 110.0])

    def test_cancelling_a_non_best_level_keeps_best(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 1)
        book.insert(2, B, 99.0, 1)
        book.insert(3, B, 98.0, 1)
        book.cancel(2)
        self.assertEqual(book.best_bid(), (100.0, 1))
        self.assertEqual([lvl[0] for lvl in book.depth(B)], [100.0, 98.0])

    def test_large_gap_between_levels(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 5)
        book.insert(2, B, 2500.0, 5)                         # 240,000 ticks above
        self.assertEqual([lvl[0] for lvl in book.depth(B)], [2500.0, 100.0])
        book.cancel(2)                                       # best level empties: scan finds 100.0
        self.assertEqual(book.best_bid(), (100.0, 5))
        self.assertTrue(book.check_integrity())

    def test_new_best_inserted_between_existing_levels(self):
        book = OrderBook()
        for oid, price in ((1, 90.0), (2, 95.0), (3, 92.5)):
            book.insert(oid, S, price, 1)
        self.assertEqual(book.best_ask(), (90.0, 1))
        self.assertEqual([lvl[0] for lvl in book.depth(S)], [90.0, 92.5, 95.0])

    def test_sequential_asks_then_cancel_best_one_by_one(self):
        book = OrderBook()
        for i in range(1000):
            book.insert(i + 1, S, 100.0 + i * 0.01, 1)
        for i in range(1000):
            self.assertAlmostEqual(book.best_ask()[0], 100.0 + i * 0.01)
            book.cancel(i + 1)
        self.assertEqual(book.level_count(S), 0)
        self.assertTrue(book.check_integrity())


class TestTableGrowth(unittest.TestCase):
    def test_window_grows_in_both_directions_and_keeps_old_levels(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 1)
        book.insert(2, B, 5000.0, 2)                         # grows upward
        book.insert(3, B, 0.5, 3)                            # grows downward
        self.assertEqual([lvl[:2] for lvl in book.depth(B)], [(5000.0, 2), (100.0, 1), (0.5, 3)])
        for oid, price, qty in ((1, 100.0, 1), (2, 5000.0, 2), (3, 0.5, 3)):
            self.assertEqual(book.level_orders(B, price), [(oid, qty)])
        self.assertTrue(book.check_integrity())

    def test_each_side_has_its_own_window(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 1)
        book.insert(2, S, 30000.0, 1)                        # far from the bids: separate table
        self.assertEqual((book.best_bid(), book.best_ask()), ((100.0, 1), (30000.0, 1)))
        self.assertTrue(book.check_integrity())

    def test_price_too_far_from_the_rest_is_rejected(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 1)
        with self.assertRaises(ValueError):
            book.insert(2, B, 100000.0, 1)                   # 9.99M ticks away (limit is 4.19M)
        self.assertEqual(len(book), 1)
        self.assertEqual(book.best_bid(), (100.0, 1))
        self.assertTrue(book.check_integrity())

    def test_levels_survive_many_growths(self):
        book = OrderBook()
        rng = random.Random(8)
        prices = {}
        for oid in range(1, 3001):
            price = round(rng.randint(100, 200000) / 100, 2)   # 1.00 .. 2000.00
            book.insert(oid, B, price, 1)
            prices[price] = prices.get(price, 0) + 1
        expected = sorted(prices, reverse=True)
        self.assertEqual([lvl[0] for lvl in book.depth(B, 10_000)], expected)
        self.assertTrue(book.check_integrity())


class TestAgainstModel(unittest.TestCase):
    """Random insert/cancel/submit on a wide price range compared with a plain Python model."""

    def test_many_levels_random_operations(self):
        rng = random.Random(2026)
        book = OrderBook()
        live = {}                                # id -> (side, ticks, qty)
        for oid in range(1, 30001):
            if live and rng.random() < 0.4:
                victim = next(iter(live))
                live.pop(victim)
                self.assertTrue(book.cancel(victim))
            else:
                side = rng.choice((B, S))
                # bids below 100.00, asks above 100.00, so nothing crosses and every level stays visible
                ticks = rng.randint(5000, 9999) if side == B else rng.randint(10001, 15000)
                qty = rng.randint(1, 50)
                book.insert(oid, side, ticks / 100, qty)
                live[oid] = (side, ticks, qty)
            if oid % 3000 == 0:
                self.assertTrue(book.check_integrity())
        for side in (B, S):
            levels = {}
            for s_, t, q in live.values():
                if s_ == side:
                    qty_sum, count = levels.get(t, (0, 0))
                    levels[t] = (qty_sum + q, count + 1)
            expected = [(t / 100, q, c) for t, (q, c) in sorted(levels.items(), reverse=(side == B))]
            self.assertEqual(book.depth(side, 100_000), expected)
            self.assertEqual(book.level_count(side), len(levels))
        self.assertEqual(len(book), len(live))


class TestBenchmarkWithManyLevels(unittest.TestCase):
    def test_benchmark_runs_for_few_and_many_levels(self):
        for levels in (50, 5000, 50000):
            ins, can = bench_insert_cancel(20000, levels)
            self.assertGreater(ins, 0)
            self.assertGreater(can, 0)


if __name__ == "__main__":
    unittest.main()
