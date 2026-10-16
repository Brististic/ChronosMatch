"""Day 6 tests: the matching loop runs without the GIL, safely.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import random
import struct
import threading
import time
import unittest

from chronos_engine import OrderBook, gil_check_demo

B, S = b"B", b"S"
WIRE = struct.Struct("=QcdIQ")          # same layout as Member 1's ring buffer messages
WIRE_SIZE = WIRE.size                   # 29 bytes


def pack_orders(orders):
    """[(id, side, price, qty), ...] -> one bytes object of 29-byte wire messages."""
    buf = bytearray(len(orders) * WIRE_SIZE)
    for i, (oid, side, price, qty) in enumerate(orders):
        WIRE.pack_into(buf, i * WIRE_SIZE, oid, side, price, qty, i)
    return bytes(buf)


def random_orders(seed, count, first_id=1, spread=8):
    rng = random.Random(seed)
    return [(first_id + i, rng.choice((B, S)), 100 + rng.randint(-spread, spread) * 0.5,
             rng.randint(1, 100)) for i in range(count)]


class TestGilIsReleased(unittest.TestCase):
    def test_gil_check_inside_nogil_block(self):
        outside, inside = gil_check_demo()
        self.assertEqual((outside, inside), (1, 0))     # held outside, released inside

    def test_single_submit_matches_without_gil(self):
        book = OrderBook()
        self.assertEqual(book.gil_held_in_last_match, -1)
        book.submit(1, S, 100.0, 10)
        self.assertEqual(book.gil_held_in_last_match, 0)

    def test_batch_matches_without_gil(self):
        book = OrderBook()
        book.submit_batch(pack_orders(random_orders(1, 100)))
        self.assertEqual(book.gil_held_in_last_match, 0)

    def test_other_python_thread_keeps_running_during_a_batch(self):
        data = pack_orders(random_orders(5, 400_000))
        progress = []
        for _attempt in range(3):                       # retry: thread scheduling is not exact
            book = OrderBook()
            counter = [0]
            stop = threading.Event()

            def spin():
                while not stop.is_set():
                    counter[0] += 1

            t = threading.Thread(target=spin)
            t.start()
            time.sleep(0.05)                            # let the spinner get going
            before = counter[0]
            book.submit_batch(data, collect=False)
            after = counter[0]
            stop.set()
            t.join()
            progress.append(after - before)
            if after - before > 1000:
                break
        self.assertGreater(max(progress), 1000,
                           "the Python thread made no progress while the engine matched: " + str(progress))


class TestBatchSubmit(unittest.TestCase):
    def test_batch_gives_same_fills_and_book_as_one_by_one(self):
        orders = random_orders(3, 5000)
        one_by_one = OrderBook()
        expected = []
        for o in orders:
            expected.extend(one_by_one.submit(*o))
        batch = OrderBook()
        result = batch.submit_batch(pack_orders(orders))
        self.assertEqual(result["fills"], expected)
        self.assertEqual(result["fill_count"], len(expected))
        self.assertEqual(result["filled_quantity"], sum(f[3] for f in expected))
        self.assertEqual((result["orders"], result["rejected"]), (5000, 0))
        for side in (B, S):
            self.assertEqual(batch.depth(side, 500), one_by_one.depth(side, 500))
        self.assertTrue(batch.check_integrity())

    def test_collect_false_gives_same_totals_without_fill_list(self):
        orders = random_orders(4, 3000)
        a = OrderBook().submit_batch(pack_orders(orders), collect=True)
        b = OrderBook().submit_batch(pack_orders(orders), collect=False)
        self.assertIsNone(b["fills"])
        self.assertEqual((a["fill_count"], a["filled_quantity"]), (b["fill_count"], b["filled_quantity"]))

    def test_invalid_and_duplicate_orders_are_counted_and_skipped(self):
        book = OrderBook()
        data = pack_orders([
            (1, S, 100.0, 10),        # ok, rests
            (2, b"X", 100.0, 10),     # invalid side
            (3, B, 100.0, 0),         # zero quantity
            (4, B, -5.0, 10),         # bad price
            (1, B, 90.0, 10),         # duplicate of resting id 1
            (5, B, 100.0, 4),         # ok, trades with order 1
        ])
        result = book.submit_batch(data)
        self.assertEqual((result["orders"], result["rejected"]), (6, 4))
        self.assertEqual(result["fills"], [(5, 1, 100.0, 4)])
        self.assertEqual(book.best_ask(), (100.0, 6))
        self.assertTrue(book.check_integrity())

    def test_bad_buffer_lengths(self):
        book = OrderBook()
        with self.assertRaises(ValueError):
            book.submit_batch(b"\x00" * 30)
        empty = book.submit_batch(b"")
        self.assertEqual((empty["orders"], empty["fills"]), (0, []))

    def test_works_with_bytearray_and_memoryview_slices(self):
        orders = random_orders(8, 100)
        data = bytearray(pack_orders(orders))
        r1 = OrderBook().submit_batch(data)
        r2 = OrderBook().submit_batch(memoryview(data)[: 50 * WIRE_SIZE])
        self.assertEqual(r2["orders"], 50)
        self.assertEqual(r1["orders"], 100)


class TestThreadSafety(unittest.TestCase):
    def test_two_threads_sharing_one_book_non_interacting_orders(self):
        bids = [(i + 1, B, 90.0 + (i % 50) * 0.1, 5) for i in range(40_000)]
        asks = [(100_000 + i, S, 110.0 + (i % 50) * 0.1, 5) for i in range(40_000)]
        book = OrderBook()
        errors = []

        def run(orders):
            try:
                for k in range(0, len(orders), 2000):
                    book.submit_batch(pack_orders(orders[k:k + 2000]), collect=False)
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(o,)) for o in (bids, asks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(book), 80_000)
        self.assertTrue(book.check_integrity())

    def test_two_threads_with_crossing_orders_conserve_quantity(self):
        a = random_orders(21, 20_000, first_id=1)
        b = random_orders(22, 20_000, first_id=1_000_000)
        book = OrderBook()
        results = []

        def run(orders):
            total = 0
            for k in range(0, len(orders), 500):
                r = book.submit_batch(pack_orders(orders[k:k + 500]), collect=False)
                self.assertEqual(r["rejected"], 0)
                total += r["filled_quantity"]
            results.append(total)

        threads = [threading.Thread(target=run, args=(o,)) for o in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        submitted = sum(o[3] for o in a) + sum(o[3] for o in b)
        resting = sum(q for side in (B, S) for _, q, _ in book.depth(side, 10_000))
        self.assertEqual(submitted, 2 * sum(results) + resting)
        self.assertTrue(book.check_integrity())

    def test_reader_thread_during_a_batch_does_not_crash_or_see_corruption(self):
        book = OrderBook()
        data = pack_orders(random_orders(6, 200_000))
        stop = threading.Event()
        errors = []

        def reader():
            try:
                while not stop.is_set():
                    book.best_bid()
                    book.best_ask()
                    len(book)
                    book.depth(B, 5)
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        t = threading.Thread(target=reader)
        t.start()
        book.submit_batch(data, collect=False)
        stop.set()
        t.join()
        self.assertEqual(errors, [])
        self.assertTrue(book.check_integrity())

    def test_independent_books_in_parallel_threads_are_deterministic(self):
        orders = random_orders(9, 20_000)
        data = pack_orders(orders)
        expected = OrderBook()
        expected_result = expected.submit_batch(data, collect=False)
        books, results = [OrderBook() for _ in range(4)], [None] * 4

        def run(i):
            results[i] = books[i].submit_batch(data, collect=False)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for book, result in zip(books, results):
            self.assertEqual(result, expected_result)
            for side in (B, S):
                self.assertEqual(book.depth(side, 500), expected.depth(side, 500))


if __name__ == "__main__":
    unittest.main()
