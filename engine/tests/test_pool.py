"""Day 12 tests: the order book gets its nodes and price levels from pre-allocated memory pools.

pool_stats() shows, per pool: capacity (reserved), in_use, peak, slabs and system_allocations (the number
of malloc/realloc calls the pool has ever made). Once the pools are big enough, system_allocations must stop
changing: insert, cancel and matching then never call the system allocator.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import os
import random
import shutil
import struct
import tempfile
import unittest

from chronos_engine import OrderBook, RingConsumer, bench_latency

B, S, C = b"B", b"S", b"C"


class TestPoolBasics(unittest.TestCase):
    def test_defaults_are_reserved_up_front(self):
        st = OrderBook().pool_stats()
        self.assertEqual((st["orders"]["capacity"], st["orders"]["in_use"], st["orders"]["slabs"]), (1024, 0, 1))
        self.assertEqual((st["levels"]["capacity"], st["levels"]["in_use"]), (128, 0))
        self.assertTrue(st["grow"])
        self.assertEqual(st["orders"]["element_bytes"], 48)           # one OrderNode

    def test_custom_capacity(self):
        st = OrderBook(order_capacity=5000, level_capacity=300).pool_stats()
        self.assertEqual((st["orders"]["capacity"], st["levels"]["capacity"]), (5000, 300))

    def test_bad_capacities_are_refused(self):
        for kwargs in ({"order_capacity": 0}, {"level_capacity": 0}, {"order_capacity": 10 ** 10},
                       {"level_capacity": 10 ** 9}):
            with self.assertRaises(ValueError):
                OrderBook(**kwargs)

    def test_in_use_follows_inserts_cancels_and_fills(self):
        book = OrderBook()
        book.insert(1, B, 99.0, 10)
        book.insert(2, B, 99.0, 10)
        book.insert(3, S, 101.0, 10)
        st = book.pool_stats()
        self.assertEqual((st["orders"]["in_use"], st["levels"]["in_use"]), (3, 2))
        book.cancel(2)
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 2)
        book.submit(4, B, 101.0, 10)                                    # fills order 3 completely
        st = book.pool_stats()
        self.assertEqual((st["orders"]["in_use"], st["levels"]["in_use"]), (1, 1))
        self.assertEqual(st["orders"]["peak"], 3)
        self.assertTrue(book.check_integrity())

    def test_double_cancel_returns_the_element_only_once(self):
        book = OrderBook()
        book.insert(1, B, 99.0, 10)
        self.assertTrue(book.cancel(1))
        self.assertFalse(book.cancel(1))
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 0)
        self.assertTrue(book.check_integrity())

    def test_rejected_orders_do_not_use_the_pool(self):
        book = OrderBook()
        book.insert(1, B, 99.0, 10)
        for args in ((2, B, 99.0, 0), (3, B, 99.005, 5), (1, B, 98.0, 5)):
            with self.assertRaises(ValueError):
                book.insert(*args)
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 1)
        self.assertEqual(book.pool_stats()["levels"]["in_use"], 1)


class TestNoSystemAllocationInSteadyState(unittest.TestCase):
    def test_a_million_insert_cancel_cycles_never_ask_the_system_for_memory(self):
        book = OrderBook()                                              # room for 1,024 orders
        for i in range(500):                                            # warm-up: some resting orders
            book.insert(i + 1, B if i % 2 else S, 50.0 + (i % 40) * 0.01 + (0 if i % 2 else 5.0), 10)
        before = book.pool_stats()
        next_id = 10_000
        for cycle in range(200_000):
            book.insert(next_id, B, 40.0 + (cycle % 30) * 0.01, 5)
            book.cancel(next_id)
            next_id += 1
        after = book.pool_stats()
        self.assertEqual(after["orders"]["system_allocations"], before["orders"]["system_allocations"])
        self.assertEqual(after["levels"]["system_allocations"], before["levels"]["system_allocations"])
        self.assertEqual(after["orders"]["slabs"], 1)
        self.assertEqual(after["orders"]["in_use"], before["orders"]["in_use"])

    def test_matching_traffic_within_capacity_never_grows_the_pools(self):
        book = OrderBook(order_capacity=4096, level_capacity=256)
        rng = random.Random(7)
        for oid in range(1, 60_001):
            book.submit(oid, rng.choice((B, S)), 100 + rng.randint(-10, 10) * 0.25, rng.randint(1, 50))
            if oid % 1000 == 0:
                st = book.pool_stats()
                if st["orders"]["in_use"] > 3000:                       # keep the book inside the reserved size
                    for victim in [x for x in range(oid - 999, oid + 1) if x in book][:600]:
                        book.cancel(victim)
        st = book.pool_stats()
        self.assertEqual(st["orders"]["slabs"], 1)
        self.assertEqual(st["orders"]["system_allocations"], 2)          # one slab + the slab list, at creation
        self.assertTrue(book.check_integrity())


class TestGrowth(unittest.TestCase):
    def test_pool_grows_by_adding_slabs_and_everything_still_works(self):
        book = OrderBook(order_capacity=1024, level_capacity=16)         # far too small on purpose
        for i in range(1, 10_001):
            book.insert(i, B, 10.0 + (i % 500) * 0.01, 3)
        st = book.pool_stats()
        self.assertGreaterEqual(st["orders"]["capacity"], 10_000)
        self.assertGreater(st["orders"]["slabs"], 1)
        self.assertGreater(st["levels"]["slabs"], 1)
        self.assertEqual(st["orders"]["in_use"], 10_000)
        self.assertEqual(len(book), 10_000)
        self.assertTrue(book.check_integrity())
        for i in range(1, 10_001):
            self.assertTrue(book.cancel(i))
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 0)
        self.assertTrue(book.check_integrity())

    def test_freed_elements_are_reused_before_a_new_slab_is_added(self):
        book = OrderBook(order_capacity=1024)
        for i in range(1, 1025):
            book.insert(i, B, 10.0, 1)
        self.assertEqual(book.pool_stats()["orders"]["slabs"], 1)
        book.cancel(5)
        book.insert(5000, B, 10.0, 1)                                    # takes the freed element
        self.assertEqual(book.pool_stats()["orders"]["slabs"], 1)
        book.insert(5001, B, 10.0, 1)                                    # now really full: new slab
        self.assertEqual(book.pool_stats()["orders"]["slabs"], 2)
        self.assertTrue(book.check_integrity())


class TestFixedSizePools(unittest.TestCase):
    def test_full_order_pool_refuses_new_orders_and_recovers_after_a_cancel(self):
        book = OrderBook(order_capacity=100, level_capacity=200, grow=False)   # 100 orders at 100 prices: levels must fit too
        for i in range(1, 101):
            book.insert(i, B, 50.0 + i * 0.01, 1)
        with self.assertRaises(MemoryError):
            book.insert(101, B, 50.0, 1)
        with self.assertRaises(MemoryError):
            book.submit(102, S, 200.0, 1)
        self.assertEqual(len(book), 100)
        self.assertEqual(book.pool_stats()["orders"]["capacity"], 100)
        self.assertTrue(book.check_integrity())
        self.assertTrue(book.cancel(7))
        book.insert(103, B, 50.0, 1)                                    # the freed element is available again
        self.assertEqual(len(book), 100)
        self.assertTrue(book.check_integrity())

    def test_full_level_pool_refuses_a_new_price_but_not_more_orders_at_existing_prices(self):
        book = OrderBook(order_capacity=1000, level_capacity=2, grow=False)
        book.insert(1, B, 50.0, 1)
        book.insert(2, B, 51.0, 1)
        with self.assertRaises(MemoryError):
            book.insert(3, B, 52.0, 1)                                   # a third price level does not fit
        book.insert(4, B, 51.0, 1)                                       # same price: no new level needed
        self.assertEqual(len(book), 3)
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 3)       # the failed insert gave its node back
        self.assertTrue(book.check_integrity())

    def test_matching_frees_room_for_the_remainder_in_a_full_pool(self):
        book = OrderBook(order_capacity=2, level_capacity=8, grow=False)
        book.insert(1, S, 100.0, 10)
        book.insert(2, S, 101.0, 10)                                     # pool is full
        fills = book.submit(3, B, 101.0, 25)                             # uses up both, remainder 5 rests
        self.assertEqual([(f[1], f[3]) for f in fills], [(1, 10), (2, 10)])
        self.assertEqual(book.best_bid(), (101.0, 5))
        self.assertEqual(book.pool_stats()["orders"]["in_use"], 1)
        self.assertTrue(book.check_integrity())

    def test_ring_path_counts_orders_that_do_not_fit_as_rejected_and_carries_on(self):
        d = tempfile.mkdtemp(prefix="chronos_pool_test_")
        try:
            ring_path = os.path.join(d, "r.bin")
            cap = 64
            with open(ring_path, "wb") as f:
                f.write(b"\x00" * (24 + cap * 29))
            wire = struct.Struct("<QcdIQ")
            with open(ring_path, "r+b") as f:
                f.write(struct.pack("<IIIIQ", 0, 0, cap, 29, 0))
                f.seek(24)
                for i in range(1, 41):
                    f.write(wire.pack(i, B, 50.0 + i * 0.01, 1, 0))
                f.seek(0)
                f.write(struct.pack("<I", 40))                             # head = 40: forty messages waiting
            book = OrderBook(order_capacity=25, level_capacity=64, grow=False)
            with RingConsumer(ring_path) as consumer:
                r = consumer.drain_into(book)
            self.assertEqual((r["messages"], r["submitted"], r["rejected"]), (40, 25, 15))
            self.assertEqual(len(book), 25)
            self.assertTrue(book.check_integrity())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSameResultsWithAnyPoolSetting(unittest.TestCase):
    def test_tiny_default_and_huge_pools_give_identical_fills_and_books(self):
        rng = random.Random(21)
        stream = [(oid, rng.choice((B, S)), 100 + rng.randint(-12, 12) * 0.25, rng.randint(1, 80)) for oid in range(1, 20_001)]
        results = []
        for kwargs in ({"order_capacity": 1, "level_capacity": 1}, {}, {"order_capacity": 50_000, "level_capacity": 4000}):
            book = OrderBook(**kwargs)
            fills = []
            for order in stream:
                fills.extend(book.submit(*order))
            results.append((fills, book.depth(B, 1000), book.depth(S, 1000), len(book)))
            self.assertTrue(book.check_integrity())
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])


class TestLifecycle(unittest.TestCase):
    def test_many_large_books_can_be_created_and_destroyed(self):
        for _ in range(30):
            book = OrderBook(order_capacity=200_000, level_capacity=10_000)
            for i in range(1, 501):
                book.insert(i, B, 10.0 + (i % 50) * 0.01, 1)
            del book

    def test_book_with_resting_orders_can_be_destroyed(self):
        for _ in range(50):
            book = OrderBook()
            for i in range(1, 2001):
                book.insert(i, B if i % 2 else S, 100.0 + (i % 100) * 0.01 + (0 if i % 2 else 5.0), 1)
            del book


class TestLatencyBenchmark(unittest.TestCase):
    def test_distribution_is_reported_and_ordered(self):
        result = bench_latency(20_000, 200)
        for op in ("insert", "cancel"):
            r = result[op]
            self.assertGreater(r["p50"], 0)
            self.assertLessEqual(r["p50"], r["p90"])
            self.assertLessEqual(r["p90"], r["p99"])
            self.assertLessEqual(r["p99"], r["p99_9"])
            self.assertLessEqual(r["p99_9"], r["max"])


if __name__ == "__main__":
    unittest.main()
