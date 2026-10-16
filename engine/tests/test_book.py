"""Day 4 tests: limit order book insertion and cancellation.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import math
import random
import struct
import unittest

from chronos_engine import OrderBook, bench_insert_cancel

B, S = b"B", b"S"
WIRE_FORMAT = "=QcdIQ"


class TestInsert(unittest.TestCase):
    def test_empty_book(self):
        book = OrderBook()
        self.assertEqual(len(book), 0)
        self.assertIsNone(book.best_bid())
        self.assertIsNone(book.best_ask())
        self.assertIsNone(book.spread())
        self.assertTrue(book.check_integrity())

    def test_bids_sorted_highest_first(self):
        book = OrderBook()
        for oid, price in ((1, 100.0), (2, 102.0), (3, 101.0), (4, 99.5)):
            book.insert(oid, B, price, 10)
        self.assertEqual([lvl[0] for lvl in book.depth(B)], [102.0, 101.0, 100.0, 99.5])
        self.assertEqual(book.best_bid(), (102.0, 10))
        self.assertTrue(book.check_integrity())

    def test_asks_sorted_lowest_first(self):
        book = OrderBook()
        for oid, price in ((1, 105.0), (2, 103.0), (3, 104.0)):
            book.insert(oid, S, price, 5)
        self.assertEqual([lvl[0] for lvl in book.depth(S)], [103.0, 104.0, 105.0])
        self.assertEqual(book.best_ask(), (103.0, 5))

    def test_spread(self):
        book = OrderBook()
        book.insert(1, B, 99.5, 10)
        book.insert(2, S, 100.25, 10)
        self.assertAlmostEqual(book.spread(), 0.75)

    def test_same_price_keeps_arrival_order(self):
        book = OrderBook()
        for oid, qty in ((10, 100), (11, 200), (12, 300)):
            book.insert(oid, B, 50.0, qty)
        self.assertEqual(book.level_orders(B, 50.0), [(10, 100), (11, 200), (12, 300)])
        self.assertEqual(book.depth(B), [(50.0, 600, 3)])
        self.assertEqual(book.level_count(B), 1)

    def test_depth_limit(self):
        book = OrderBook()
        for i in range(10):
            book.insert(i + 1, B, 100.0 - i, 1)
        self.assertEqual(len(book.depth(B, 3)), 3)
        self.assertEqual(len(book.depth(B, 50)), 10)

    def test_insert_wire_message(self):
        book = OrderBook()
        book.insert_wire(struct.pack(WIRE_FORMAT, 42, B, 101.5, 700, 123456789))
        self.assertEqual(book.best_bid(), (101.5, 700))
        self.assertIn(42, book)
        with self.assertRaises(ValueError):
            book.insert_wire(b"\x00" * 5)

    def test_extreme_ids(self):
        book = OrderBook()
        for oid in (0, 1, 2**63, 2**64 - 1):
            book.insert(oid, B, 10.0 + (oid % 7), 1)
        self.assertEqual(len(book), 4)
        for oid in (0, 1, 2**63, 2**64 - 1):
            self.assertIn(oid, book)
        self.assertTrue(book.check_integrity())


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook()
        self.book.insert(1, B, 100.0, 10)

    def test_duplicate_id_rejected_even_on_other_side(self):
        with self.assertRaises(ValueError):
            self.book.insert(1, B, 100.0, 5)
        with self.assertRaises(ValueError):
            self.book.insert(1, S, 105.0, 5)
        self.assertEqual(len(self.book), 1)
        self.assertEqual(self.book.best_bid(), (100.0, 10))

    def test_bad_quantity(self):
        with self.assertRaises(ValueError):
            self.book.insert(2, B, 100.0, 0)

    def test_bad_prices(self):
        for price in (0.0, -1.0, math.nan, math.inf, -math.inf, 1e16):
            with self.assertRaises(ValueError, msg=repr(price)):
                self.book.insert(9, B, price, 1)
        self.assertEqual(len(self.book), 1)

    def test_bad_side(self):
        with self.assertRaises(ValueError):
            self.book.insert(3, b"X", 100.0, 1)
        with self.assertRaises(ValueError):
            self.book.insert(3, b"", 100.0, 1)
        with self.assertRaises(ValueError):
            self.book.depth(b"Q")

    def test_rejected_orders_leave_book_unchanged(self):
        before = (len(self.book), self.book.depth(B), self.book.depth(S))
        for args in ((2, B, 100.0, 0), (3, B, -5.0, 1), (4, b"Z", 1.0, 1), (1, B, 1.0, 1)):
            with self.assertRaises(ValueError):
                self.book.insert(*args)
        self.assertEqual((len(self.book), self.book.depth(B), self.book.depth(S)), before)
        self.assertTrue(self.book.check_integrity())


class TestCancel(unittest.TestCase):
    def test_cancel_reduces_level(self):
        book = OrderBook()
        book.insert(1, B, 100.0, 100)
        book.insert(2, B, 100.0, 50)
        self.assertTrue(book.cancel(1))
        self.assertEqual(book.depth(B), [(100.0, 50, 1)])
        self.assertNotIn(1, book)
        self.assertTrue(book.check_integrity())

    def test_cancelling_last_order_removes_level_and_updates_best(self):
        book = OrderBook()
        book.insert(1, B, 102.0, 10)
        book.insert(2, B, 101.0, 20)
        self.assertEqual(book.best_bid(), (102.0, 10))
        self.assertTrue(book.cancel(1))
        self.assertEqual(book.best_bid(), (101.0, 20))
        self.assertEqual(book.level_count(B), 1)
        self.assertTrue(book.cancel(2))
        self.assertIsNone(book.best_bid())
        self.assertEqual(book.level_count(B), 0)

    def test_cancel_unknown_and_double_cancel(self):
        book = OrderBook()
        book.insert(1, S, 100.0, 10)
        self.assertFalse(book.cancel(999))
        self.assertTrue(book.cancel(1))
        self.assertFalse(book.cancel(1))

    def test_cancel_head_middle_tail_preserves_order_of_rest(self):
        book = OrderBook()
        for oid in range(1, 7):
            book.insert(oid, S, 100.0, oid * 10)
        book.cancel(1)   # head
        book.cancel(4)   # middle
        book.cancel(6)   # tail
        self.assertEqual(book.level_orders(S, 100.0), [(2, 20), (3, 30), (5, 50)])
        self.assertTrue(book.check_integrity())

    def test_id_can_be_reused_after_cancel(self):
        book = OrderBook()
        for _ in range(5):
            book.insert(7, B, 10.0, 1)
            self.assertTrue(book.cancel(7))
        book.insert(7, S, 20.0, 2)
        self.assertEqual(book.best_ask(), (20.0, 2))
        self.assertIsNone(book.best_bid())

    def test_cancel_in_either_side_does_not_touch_other_side(self):
        book = OrderBook()
        book.insert(1, B, 99.0, 10)
        book.insert(2, S, 101.0, 10)
        book.cancel(2)
        self.assertEqual(book.best_bid(), (99.0, 10))
        self.assertIsNone(book.best_ask())


class TestScale(unittest.TestCase):
    def test_100k_orders_insert_then_cancel_all(self):
        book = OrderBook()
        for i in range(1, 100_001):
            side = B if i % 2 else S
            price = (100.0 - (i % 500) * 0.01) if side == B else (101.0 + (i % 500) * 0.01)
            book.insert(i, side, price, 10)
        self.assertEqual(len(book), 100_000)
        self.assertTrue(book.check_integrity())
        for i in range(1, 100_001):
            self.assertTrue(book.cancel(i))
        self.assertEqual(len(book), 0)
        self.assertEqual((book.level_count(B), book.level_count(S)), (0, 0))
        self.assertTrue(book.check_integrity())

    def test_insert_cancel_churn_with_tombstones(self):
        book = OrderBook()
        for round_no in range(50):
            base = round_no * 1000
            for i in range(1, 1001):
                book.insert(base + i, B, 50.0 + (i % 10), 1)
            for i in range(1, 1001):
                book.cancel(base + i)
        self.assertEqual(len(book), 0)
        self.assertTrue(book.check_integrity())

    def test_many_books_created_and_destroyed(self):
        for _ in range(100):
            book = OrderBook()
            for i in range(200):
                book.insert(i + 1, B if i % 2 else S, 10.0 + i % 20, 3)
            del book


class TestAgainstReferenceModel(unittest.TestCase):
    """Differential test: random insert/cancel vs a simple Python model."""

    def test_random_operations(self):
        rng = random.Random(4)
        book = OrderBook()
        live = {}                       # id -> (side, price, qty)
        order_in_level = {}             # (side, price) -> [ids in arrival order]
        next_id = 1
        for step in range(20000):
            if live and rng.random() < 0.45:
                oid = rng.choice(list(live)) if step % 50 == 0 else next(iter(live))
                side, price, qty = live.pop(oid)
                order_in_level[(side, price)].remove(oid)
                self.assertTrue(book.cancel(oid))
            else:
                side = rng.choice((B, S))
                price = round(100 + (rng.randint(0, 40) - 20) * 0.25, 2)
                qty = rng.randint(1, 500)
                book.insert(next_id, side, price, qty)
                live[next_id] = (side, price, qty)
                order_in_level.setdefault((side, price), []).append(next_id)
                next_id += 1
            if step % 500 == 0:
                self.assertTrue(book.check_integrity())
        self.assertEqual(len(book), len(live))
        for side in (B, S):
            prices = sorted({p for (s, p), ids in order_in_level.items() if s == side and ids},
                            reverse=(side == B))
            expected = [(p, sum(live[i][2] for i in order_in_level[(side, p)]),
                         len(order_in_level[(side, p)])) for p in prices]
            self.assertEqual(book.depth(side, 1000), expected)
            for p in prices:
                self.assertEqual([x[0] for x in book.level_orders(side, p)], order_in_level[(side, p)])
        self.assertTrue(book.check_integrity())


class TestBenchmark(unittest.TestCase):
    def test_c_level_benchmark_runs(self):
        per_insert_ns, per_cancel_ns = bench_insert_cancel(20000)
        self.assertGreater(per_insert_ns, 0)
        self.assertGreater(per_cancel_ns, 0)


if __name__ == "__main__":
    unittest.main()
