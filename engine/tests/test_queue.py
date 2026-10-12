"""Day 3 tests: price-time priority queue for one price level.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import collections
import random
import unittest

from chronos_engine import LevelQueue, is_better_price


class TestTimePriority(unittest.TestCase):
    def test_fifo_order(self):
        q = LevelQueue(100.5)
        for i in range(1, 6):
            q.push(i, i * 10)
        self.assertEqual(q.orders(), [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)])
        self.assertEqual([q.pop()[0] for _ in range(5)], [1, 2, 3, 4, 5])
        self.assertIsNone(q.pop())

    def test_arrival_order_beats_timestamp_field(self):
        q = LevelQueue(50.0)
        q.push(1, 10, timestamp_ns=999)   # arrived first, larger timestamp
        q.push(2, 10, timestamp_ns=1)
        self.assertEqual(q.pop()[0], 1)

    def test_peek_does_not_remove(self):
        q = LevelQueue(1.0)
        self.assertIsNone(q.peek())
        q.push(7, 70)
        self.assertEqual(q.peek(), (7, 70))
        self.assertEqual(len(q), 1)


class TestCounters(unittest.TestCase):
    def test_totals_follow_push_pop_remove(self):
        q = LevelQueue(10.0)
        q.push(1, 100)
        q.push(2, 250)
        q.push(3, 50)
        self.assertEqual((q.order_count, q.total_quantity), (3, 400))
        q.pop()
        self.assertEqual((q.order_count, q.total_quantity), (2, 300))
        self.assertTrue(q.remove(3))
        self.assertEqual((q.order_count, q.total_quantity), (1, 250))
        self.assertTrue(q.check_integrity())

    def test_price_property(self):
        self.assertEqual(LevelQueue(123.45).price, 123.45)


class TestRemove(unittest.TestCase):
    def setUp(self):
        self.q = LevelQueue(10.0)
        for i in (1, 2, 3, 4):
            self.q.push(i, 10)

    def test_remove_head_middle_tail(self):
        self.assertTrue(self.q.remove(1))   # head
        self.assertTrue(self.q.remove(3))   # middle
        self.assertTrue(self.q.remove(4))   # tail
        self.assertEqual(self.q.orders(), [(2, 10)])
        self.assertTrue(self.q.check_integrity())

    def test_remove_unknown_id(self):
        self.assertFalse(self.q.remove(99))
        self.assertEqual(len(self.q), 4)

    def test_queue_usable_after_becoming_empty(self):
        for i in (1, 2, 3, 4):
            self.assertTrue(self.q.remove(i))
        self.assertEqual((len(self.q), self.q.total_quantity), (0, 0))
        self.assertTrue(self.q.check_integrity())
        self.q.push(8, 80)
        self.assertEqual(self.q.peek(), (8, 80))
        self.assertTrue(self.q.check_integrity())


class TestValidation(unittest.TestCase):
    def test_zero_quantity_rejected(self):
        with self.assertRaises(ValueError):
            LevelQueue(1.0).push(1, 0)

    def test_bad_side_rejected(self):
        with self.assertRaises(ValueError):
            LevelQueue(1.0).push(1, 5, side=b"X")


class TestPricePriority(unittest.TestCase):
    def test_buyers_prefer_higher_price(self):
        self.assertTrue(is_better_price(b"B", 101.0, 100.0))
        self.assertFalse(is_better_price(b"B", 100.0, 101.0))

    def test_sellers_prefer_lower_price(self):
        self.assertTrue(is_better_price(b"S", 99.0, 100.0))
        self.assertFalse(is_better_price(b"S", 100.0, 99.0))

    def test_equal_price_has_no_priority(self):
        self.assertFalse(is_better_price(b"B", 100.0, 100.0))
        self.assertFalse(is_better_price(b"S", 100.0, 100.0))

    def test_invalid_side(self):
        with self.assertRaises(ValueError):
            is_better_price(b"Z", 1.0, 2.0)


class TestAgainstReferenceModel(unittest.TestCase):
    """Differential test: random operations vs a plain Python deque."""

    def test_random_operations(self):
        rng = random.Random(2026)
        q = LevelQueue(100.0)
        ref = collections.deque()
        next_id = 1
        for step in range(5000):
            op = rng.choice(("push", "push", "pop", "remove"))
            if op == "push":
                qty = rng.randint(1, 1000)
                q.push(next_id, qty)
                ref.append((next_id, qty))
                next_id += 1
            elif op == "pop":
                expected = ref.popleft() if ref else None
                self.assertEqual(q.pop(), expected)
            elif ref:
                victim = rng.choice(ref)
                ref.remove(victim)
                self.assertTrue(q.remove(victim[0]))
            if step % 250 == 0:
                self.assertTrue(q.check_integrity())
        self.assertEqual(q.orders(), list(ref))
        self.assertEqual(q.total_quantity, sum(x[1] for x in ref))
        self.assertTrue(q.check_integrity())


class TestMemory(unittest.TestCase):
    def test_create_and_destroy_many_queues_with_orders(self):
        for _ in range(200):
            q = LevelQueue(1.0)
            for i in range(100):
                q.push(i + 1, 5)
            del q


if __name__ == "__main__":
    unittest.main()
