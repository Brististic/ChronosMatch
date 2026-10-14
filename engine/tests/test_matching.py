"""Day 5 test fixtures: deterministic matching of crossing Buy and Sell limit orders.

Each test builds a small, named book (the fixture), submits an order and checks the exact
fills and the book that is left. Fills are (taker_id, maker_id, price, quantity).

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import random
import unittest

from chronos_engine import OrderBook, bench_match

B, S = b"B", b"S"


def make_book(*resting):
    """Fixture helper: rest orders given as (id, side, price, qty) without matching."""
    book = OrderBook()
    for oid, side, price, qty in resting:
        book.insert(oid, side, price, qty)
    return book


class MatchingTestCase(unittest.TestCase):
    def tearDown(self):
        book = getattr(self, "book", None)
        if book is not None:
            self.assertTrue(book.check_integrity())
            bid, ask = book.best_bid(), book.best_ask()
            if bid and ask:
                self.assertLess(bid[0], ask[0], "book must never be left crossed")


class TestNoTrade(MatchingTestCase):
    def test_non_crossing_orders_rest(self):
        self.book = make_book((1, S, 100.0, 10))
        self.assertEqual(self.book.submit(2, B, 99.0, 10), [])
        self.assertEqual(self.book.best_bid(), (99.0, 10))
        self.assertEqual(self.book.best_ask(), (100.0, 10))
        self.assertAlmostEqual(self.book.spread(), 1.0)

    def test_empty_opposite_side_rests(self):
        self.book = OrderBook()
        self.assertEqual(self.book.submit(1, S, 100.0, 5), [])
        self.assertEqual(self.book.best_ask(), (100.0, 5))


class TestExactMatch(MatchingTestCase):
    def test_buy_matches_resting_sell_exactly(self):
        self.book = make_book((1, S, 100.0, 50))
        self.assertEqual(self.book.submit(2, B, 100.0, 50), [(2, 1, 100.0, 50)])
        self.assertEqual(len(self.book), 0)
        self.assertIsNone(self.book.best_ask())

    def test_sell_matches_resting_buy_exactly(self):
        self.book = make_book((1, B, 100.0, 50))
        self.assertEqual(self.book.submit(2, S, 100.0, 50), [(2, 1, 100.0, 50)])
        self.assertEqual(len(self.book), 0)

    def test_equal_prices_cross(self):
        self.book = make_book((1, S, 100.0, 10))
        self.assertEqual(len(self.book.submit(2, B, 100.0, 10)), 1)

    def test_filled_orders_leave_the_id_table(self):
        self.book = make_book((1, S, 100.0, 10))
        self.book.submit(2, B, 100.0, 10)
        self.assertNotIn(1, self.book)
        self.assertNotIn(2, self.book)
        self.assertFalse(self.book.cancel(1))


class TestExecutionPrice(MatchingTestCase):
    def test_buyer_trades_at_resting_ask_price(self):
        self.book = make_book((1, S, 100.0, 10))
        self.assertEqual(self.book.submit(2, B, 105.0, 10), [(2, 1, 100.0, 10)])

    def test_seller_trades_at_resting_bid_price(self):
        self.book = make_book((1, B, 100.0, 10))
        self.assertEqual(self.book.submit(2, S, 95.0, 10), [(2, 1, 100.0, 10)])


class TestPriceTimePriority(MatchingTestCase):
    def test_best_price_first_for_buyer(self):
        self.book = make_book((1, S, 101.0, 10), (2, S, 100.0, 10), (3, S, 102.0, 10))
        fills = self.book.submit(9, B, 103.0, 30)
        self.assertEqual(fills, [(9, 2, 100.0, 10), (9, 1, 101.0, 10), (9, 3, 102.0, 10)])
        self.assertEqual(len(self.book), 0)

    def test_best_price_first_for_seller(self):
        self.book = make_book((1, B, 99.0, 10), (2, B, 101.0, 10), (3, B, 100.0, 10))
        fills = self.book.submit(9, S, 98.0, 30)
        self.assertEqual([f[1] for f in fills], [2, 3, 1])
        self.assertEqual([f[2] for f in fills], [101.0, 100.0, 99.0])

    def test_oldest_order_first_at_same_price(self):
        self.book = make_book((1, S, 100.0, 100), (2, S, 100.0, 100), (3, S, 100.0, 100))
        fills = self.book.submit(9, B, 100.0, 250)
        self.assertEqual(fills, [(9, 1, 100.0, 100), (9, 2, 100.0, 100), (9, 3, 100.0, 50)])
        self.assertEqual(self.book.level_orders(S, 100.0), [(3, 50)])

    def test_later_insert_does_not_jump_the_queue(self):
        self.book = make_book((1, S, 100.0, 10))
        self.book.submit(2, S, 100.0, 10)      # same price, arrives later
        self.assertEqual(self.book.submit(3, B, 100.0, 10), [(3, 1, 100.0, 10)])
        self.assertEqual(self.book.level_orders(S, 100.0), [(2, 10)])


class TestPartialFills(MatchingTestCase):
    def test_taker_remainder_rests_on_the_book(self):
        self.book = make_book((1, S, 100.0, 30))
        self.assertEqual(self.book.submit(2, B, 100.0, 100), [(2, 1, 100.0, 30)])
        self.assertEqual(self.book.best_bid(), (100.0, 70))
        self.assertEqual(self.book.level_orders(B, 100.0), [(2, 70)])
        self.assertIsNone(self.book.best_ask())

    def test_partly_filled_maker_keeps_its_place(self):
        self.book = make_book((1, S, 100.0, 100), (2, S, 100.0, 50))
        self.assertEqual(self.book.submit(3, B, 100.0, 40), [(3, 1, 100.0, 40)])
        self.assertEqual(self.book.level_orders(S, 100.0), [(1, 60), (2, 50)])
        self.assertEqual(self.book.best_ask(), (100.0, 110))

    def test_partly_filled_maker_can_still_be_cancelled(self):
        self.book = make_book((1, S, 100.0, 100))
        self.book.submit(2, B, 100.0, 40)
        self.assertTrue(self.book.cancel(1))
        self.assertEqual(len(self.book), 0)

    def test_sweep_several_levels_then_rest_remainder(self):
        self.book = make_book((1, S, 100.0, 10), (2, S, 101.0, 10))
        fills = self.book.submit(3, B, 102.0, 35)
        self.assertEqual(fills, [(3, 1, 100.0, 10), (3, 2, 101.0, 10)])
        self.assertEqual(self.book.best_bid(), (102.0, 15))
        self.assertIsNone(self.book.best_ask())

    def test_limit_price_stops_the_sweep(self):
        self.book = make_book((1, S, 100.0, 10), (2, S, 101.0, 10), (3, S, 102.0, 10))
        fills = self.book.submit(9, B, 101.0, 100)
        self.assertEqual(fills, [(9, 1, 100.0, 10), (9, 2, 101.0, 10)])
        self.assertEqual(self.book.best_ask(), (102.0, 10))      # 102 untouched
        self.assertEqual(self.book.best_bid(), (101.0, 80))      # remainder rests at its limit

    def test_sell_side_sweep_mirrors_buy_side(self):
        self.book = make_book((1, B, 100.0, 10), (2, B, 99.0, 10), (3, B, 98.0, 10))
        fills = self.book.submit(9, S, 99.0, 100)
        self.assertEqual(fills, [(9, 1, 100.0, 10), (9, 2, 99.0, 10)])
        self.assertEqual(self.book.best_bid(), (98.0, 10))
        self.assertEqual(self.book.best_ask(), (99.0, 80))


class TestValidationAndIds(MatchingTestCase):
    def test_id_resting_on_book_is_rejected_before_any_trade(self):
        self.book = make_book((1, S, 100.0, 10), (2, B, 90.0, 10))
        with self.assertRaises(ValueError):
            self.book.submit(2, B, 100.0, 10)         # id 2 is already resting
        self.assertEqual(self.book.best_ask(), (100.0, 10))   # nothing traded
        self.assertEqual(len(self.book), 2)

    def test_invalid_orders_rejected(self):
        self.book = make_book((1, S, 100.0, 10))
        for args in ((5, B, 100.0, 0), (5, B, -1.0, 1), (5, B, float("nan"), 1), (5, b"X", 1.0, 1)):
            with self.assertRaises(ValueError):
                self.book.submit(*args)
        self.assertEqual(len(self.book), 1)

    def test_fully_filled_id_may_be_used_again(self):
        self.book = make_book((1, S, 100.0, 10))
        self.book.submit(2, B, 100.0, 10)
        self.assertEqual(self.book.submit(2, S, 50.0, 5), [])    # id 2 is free again


class TestDeterminism(MatchingTestCase):
    @staticmethod
    def order_stream(seed, count):
        rng = random.Random(seed)
        for oid in range(1, count + 1):
            side = rng.choice((B, S))
            price = 100 + rng.randint(-8, 8) * 0.5
            yield oid, side, price, rng.randint(1, 100)

    def run_stream(self, seed, count):
        book = OrderBook()
        trace = []
        for order in self.order_stream(seed, count):
            trace.append(book.submit(*order))
        return book, trace

    def test_same_input_gives_same_fills_and_book(self):
        book1, trace1 = self.run_stream(7, 3000)
        book2, trace2 = self.run_stream(7, 3000)
        self.assertEqual(trace1, trace2)
        for side in (B, S):
            self.assertEqual(book1.depth(side, 500), book2.depth(side, 500))
        self.book = book1

    def test_different_input_gives_different_result(self):
        _, trace1 = self.run_stream(7, 500)
        _, trace2 = self.run_stream(8, 500)
        self.assertNotEqual(trace1, trace2)

    def test_quantity_is_conserved(self):
        book = OrderBook()
        submitted = 0
        traded = 0
        for oid, side, price, qty in self.order_stream(11, 3000):
            submitted += qty
            traded += sum(f[3] for f in book.submit(oid, side, price, qty))
        resting = sum(q for side in (B, S) for _, q, _ in book.depth(side, 1000))
        # every traded unit removes one unit from the taker AND one from the maker
        self.assertEqual(submitted, 2 * traded + resting)
        self.book = book


class RefEngine:
    """Tiny pure-Python matching engine used as the oracle."""

    def __init__(self):
        self.bids, self.asks = {}, {}

    def submit(self, oid, side, price, qty):
        fills = []
        opposite = self.asks if side == B else self.bids
        while qty > 0 and opposite:
            best = min(opposite) if side == B else max(opposite)
            if (side == B and best > price) or (side == S and best < price):
                break
            queue = opposite[best]
            while qty > 0 and queue:
                maker_id, maker_qty = queue[0]
                t = min(qty, maker_qty)
                fills.append((oid, maker_id, best, t))
                qty -= t
                if t == maker_qty:
                    queue.pop(0)
                else:
                    queue[0][1] -= t
            if not queue:
                del opposite[best]
        if qty > 0:
            (self.bids if side == B else self.asks).setdefault(price, []).append([oid, qty])
        return fills

    def depth(self, side):
        levels = self.bids if side == B else self.asks
        return [(p, sum(q for _, q in levels[p]), len(levels[p]))
                for p in sorted(levels, reverse=(side == B))]


class TestAgainstReferenceEngine(MatchingTestCase):
    def test_10000_random_orders_match_the_oracle(self):
        rng = random.Random(2026)
        self.book = OrderBook()
        ref = RefEngine()
        for oid in range(1, 10001):
            side = rng.choice((B, S))
            price = 100 + rng.randint(-10, 10) * 0.25
            qty = rng.randint(1, 120)
            self.assertEqual(self.book.submit(oid, side, price, qty), ref.submit(oid, side, price, qty),
                             msg="order %d" % oid)
            if oid % 250 == 0:
                for s in (B, S):
                    self.assertEqual(self.book.depth(s, 1000), ref.depth(s))
                self.assertTrue(self.book.check_integrity())
        for s in (B, S):
            self.assertEqual(self.book.depth(s, 1000), ref.depth(s))


class TestBenchmark(unittest.TestCase):
    def test_match_benchmark_runs(self):
        self.assertGreater(bench_match(20000), 0)


if __name__ == "__main__":
    unittest.main()
