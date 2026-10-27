"""Day 7 tests: partial order execution, remainder persistence and traceability.

submit_traced() returns {"status", "filled_quantity", "resting_quantity", "fills"} where each fill is
(trade_id, taker_id, maker_id, price, quantity, taker_remaining, maker_remaining, timestamp_ns).

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import random
import unittest

from chronos_engine import OrderBook

B, S = b"B", b"S"
TRADE_ID, TAKER, MAKER, PRICE, QTY, TAKER_LEFT, MAKER_LEFT, TS = range(8)


def make_book(*resting):
    book = OrderBook()
    for oid, side, price, qty in resting:
        book.insert(oid, side, price, qty)
    return book


class PartialTestCase(unittest.TestCase):
    def tearDown(self):
        book = getattr(self, "book", None)
        if book is not None:
            self.assertTrue(book.check_integrity())


class TestPartialExecutionOfResting(PartialTestCase):
    def test_resting_order_partly_used_keeps_the_rest(self):
        self.book = make_book((1, S, 100.0, 100))
        result = self.book.submit_traced(2, B, 100.0, 30)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["fills"][0][QTY:MAKER_LEFT + 1], (30, 0, 70))
        self.assertEqual(self.book.remaining(1), 70)
        self.assertEqual(self.book.best_ask(), (100.0, 70))

    def test_same_order_filled_in_several_steps_until_gone(self):
        self.book = make_book((1, S, 100.0, 100))
        left = []
        for taker in range(2, 6):                       # four buys of 20
            r = self.book.submit_traced(taker, B, 100.0, 20)
            left.append(r["fills"][0][MAKER_LEFT])
        self.assertEqual(left, [80, 60, 40, 20])
        r = self.book.submit_traced(6, B, 100.0, 20)
        self.assertEqual(r["fills"][0][MAKER_LEFT], 0)
        self.assertIsNone(self.book.remaining(1))
        self.assertEqual(self.book.level_count(S), 0)

    def test_partly_used_order_keeps_time_priority_over_later_arrivals(self):
        self.book = make_book((1, S, 100.0, 100), (2, S, 100.0, 100))
        self.book.submit(3, B, 100.0, 50)               # order 1 is now 50
        self.book.insert(4, S, 100.0, 10)               # arrives later
        fills = self.book.submit(5, B, 100.0, 160)
        self.assertEqual([(f[1], f[3]) for f in fills], [(1, 50), (2, 100), (4, 10)])

    def test_partly_used_order_can_be_cancelled_for_its_remainder(self):
        self.book = make_book((1, S, 100.0, 100))
        self.book.submit(2, B, 100.0, 40)
        self.assertTrue(self.book.cancel(1))
        self.assertEqual(len(self.book), 0)
        self.assertIsNone(self.book.best_ask())

    def test_level_total_follows_partial_fills(self):
        self.book = make_book((1, S, 100.0, 100), (2, S, 100.0, 50))
        self.book.submit(3, B, 100.0, 120)
        self.assertEqual(self.book.depth(S), [(100.0, 30, 1)])
        self.assertEqual(self.book.level_orders(S, 100.0), [(2, 30)])


class TestRemainderPersistence(PartialTestCase):
    def test_taker_partial_fill_status_and_resting_remainder(self):
        self.book = make_book((1, S, 100.0, 30))
        r = self.book.submit_traced(2, B, 100.0, 100)
        self.assertEqual((r["status"], r["filled_quantity"], r["resting_quantity"]), ("PARTIAL", 30, 70))
        self.assertEqual(r["fills"][0][TAKER_LEFT], 70)
        self.assertEqual(self.book.remaining(2), 70)
        self.assertEqual(self.book.best_bid(), (100.0, 70))

    def test_no_trade_means_whole_order_rests(self):
        self.book = make_book((1, S, 105.0, 10))
        r = self.book.submit_traced(2, B, 100.0, 40)
        self.assertEqual((r["status"], r["filled_quantity"], r["resting_quantity"], r["fills"]),
                         ("RESTING", 0, 40, []))

    def test_fully_filled_order_never_rests(self):
        self.book = make_book((1, S, 100.0, 50))
        r = self.book.submit_traced(2, B, 100.0, 50)
        self.assertEqual((r["status"], r["resting_quantity"]), ("FILLED", 0))
        self.assertIsNone(self.book.remaining(2))

    def test_remainder_later_acts_as_a_maker(self):
        self.book = make_book((1, S, 100.0, 30))
        self.book.submit(2, B, 100.0, 100)              # 70 rests as order 2
        r = self.book.submit_traced(3, S, 99.0, 50)     # hits the remainder
        self.assertEqual(r["fills"][0][1:], (3, 2, 100.0, 50, 0, 20, 0)[0:7])
        self.assertEqual(self.book.remaining(2), 20)

    def test_remainder_joins_the_back_of_its_level(self):
        self.book = make_book((1, S, 100.0, 30), (2, B, 99.0, 10))
        self.book.submit(3, B, 99.0, 5)                 # rests behind order 2
        self.book.submit(4, B, 100.0, 80)               # 30 trades, 50 rests at 100
        self.assertEqual(self.book.best_bid(), (100.0, 50))
        self.assertEqual(self.book.level_orders(B, 99.0), [(2, 10), (3, 5)])

    def test_sweep_trace_is_consistent(self):
        self.book = make_book((1, S, 100.0, 10), (2, S, 101.0, 20), (3, S, 102.0, 30))
        r = self.book.submit_traced(9, B, 101.0, 100)
        self.assertEqual([f[TAKER_LEFT] for f in r["fills"]], [90, 70])
        self.assertEqual([f[MAKER_LEFT] for f in r["fills"]], [0, 0])
        self.assertEqual(r["resting_quantity"], 70)
        self.assertEqual(sum(f[QTY] for f in r["fills"]) + r["resting_quantity"], 100)


class TestTraceability(PartialTestCase):
    def test_trade_ids_start_at_one_and_are_contiguous(self):
        self.book = make_book((1, S, 100.0, 10), (2, S, 100.0, 10), (3, S, 101.0, 10))
        self.assertEqual(self.book.trades_executed, 0)
        r1 = self.book.submit_traced(7, B, 101.0, 15)
        r2 = self.book.submit_traced(8, B, 101.0, 15)
        # r1: 10 from order 1 + 5 from order 2; r2: last 5 of order 2 + 10 from order 3
        ids = [f[TRADE_ID] for f in r1["fills"] + r2["fills"]]
        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertEqual(self.book.trades_executed, 4)

    def test_rejected_and_non_crossing_orders_use_no_trade_id(self):
        self.book = make_book((1, S, 100.0, 10))
        self.book.submit_traced(2, B, 90.0, 5)
        with self.assertRaises(ValueError):
            self.book.submit_traced(1, B, 100.0, 5)     # id 1 already resting
        with self.assertRaises(ValueError):
            self.book.submit_traced(3, B, 100.0, 0)
        self.assertEqual(self.book.trades_executed, 0)

    def test_fill_carries_takers_timestamp(self):
        self.book = make_book((1, S, 100.0, 10))
        r = self.book.submit_traced(2, B, 100.0, 5, timestamp_ns=123456789)
        self.assertEqual(r["fills"][0][TS], 123456789)

    def test_submit_and_submit_traced_agree(self):
        rng = random.Random(3)
        plain, traced = OrderBook(), OrderBook()
        for oid in range(1, 2001):
            side = rng.choice((B, S))
            args = (oid, side, 100 + rng.randint(-6, 6) * 0.5, rng.randint(1, 60))
            expected = plain.submit(*args)
            got = traced.submit_traced(*args)["fills"]
            self.assertEqual([(f[TAKER], f[MAKER], f[PRICE], f[QTY]) for f in got], expected)
        self.book = traced

    def test_two_books_same_input_same_trace_including_trade_ids(self):
        def run():
            rng = random.Random(12)
            book, trace = OrderBook(), []
            for oid in range(1, 3001):
                trace.append(book.submit_traced(oid, rng.choice((B, S)),
                                                100 + rng.randint(-6, 6) * 0.5, rng.randint(1, 60)))
            return book, trace
        (book1, trace1), (book2, trace2) = run(), run()
        self.assertEqual(trace1, trace2)
        self.assertEqual(book1.trades_executed, book2.trades_executed)
        self.book = book1

    def test_remaining_query(self):
        self.book = make_book((1, S, 100.0, 10))
        self.assertEqual(self.book.remaining(1), 10)
        self.assertIsNone(self.book.remaining(99))
        self.book.cancel(1)
        self.assertIsNone(self.book.remaining(1))


class TestAuditReconstruction(PartialTestCase):
    """The fills alone must be enough to rebuild exactly what is left on the book."""

    def test_ledger_rebuilt_from_fills_matches_the_book(self):
        rng = random.Random(77)
        self.book = OrderBook()
        open_qty = {}                                    # order id -> quantity not yet traded
        last_trade = 0
        for oid in range(1, 8001):
            side = rng.choice((B, S))
            price = 100 + rng.randint(-10, 10) * 0.25
            qty = rng.randint(1, 150)
            open_qty[oid] = qty
            r = self.book.submit_traced(oid, side, price, qty)
            for f in r["fills"]:
                self.assertEqual(f[TRADE_ID], last_trade + 1)          # no gaps, no repeats
                last_trade = f[TRADE_ID]
                open_qty[f[TAKER]] -= f[QTY]
                open_qty[f[MAKER]] -= f[QTY]
                self.assertEqual(f[TAKER_LEFT], open_qty[f[TAKER]])     # trace == ledger
                self.assertEqual(f[MAKER_LEFT], open_qty[f[MAKER]])
            self.assertEqual(r["filled_quantity"], sum(f[QTY] for f in r["fills"]))
            self.assertEqual(r["resting_quantity"], open_qty[oid])
        # every order: what the ledger says is left == what the book says is left
        for oid, left in open_qty.items():
            self.assertEqual(self.book.remaining(oid), left if left > 0 else None, msg="order %d" % oid)
        self.assertEqual(len(self.book), sum(1 for left in open_qty.values() if left > 0))
        self.assertEqual(self.book.trades_executed, last_trade)


if __name__ == "__main__":
    unittest.main()
