"""Day 9 tests: the engine reads orders straight from the memory-mapped ring buffer.

The ring and lock file formats are Member 1's (src/ring_buffer.py and src/ipc_sync.py):
  ring   header "<IIIIQ" (head, tail, capacity, slot_size, drops) = 24 bytes, then capacity x 29-byte slots
  lock   "<II" (flag, owner pid)
RefRing / RefLock below write exactly that format, whole-header rewrites included, so the tests do
not depend on the other members' code. If the full repository is present (a `src` folder next to
`engine`), the last test class also runs against Member 1's real classes.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import importlib
import mmap
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from chronos_engine import OrderBook, RingConsumer

B, S, C = b"B", b"S", b"C"
WIRE = struct.Struct("<QcdIQ")
HEADER = struct.Struct("<IIIIQ")
HEADER_SIZE = HEADER.size                      # 24
SLOT = WIRE.size                               # 29


def pack(oid, side, price, qty, ts=0):
    return WIRE.pack(oid, side, price, qty, ts)


class RefLock:
    """Same protocol as Member 1's MmapProcessLock (flag + pid in an 8-byte mapped file)."""

    def __init__(self, path, create=False):
        if create:
            with open(path, "wb") as f:
                f.write(b"\x00" * 8)
        self._fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._mm = mmap.mmap(self._fd, 8, access=mmap.ACCESS_WRITE)

    def state(self):
        return struct.unpack_from("<II", self._mm, 0)

    def set_state(self, flag, pid):
        struct.pack_into("<II", self._mm, 0, flag, pid)

    def acquire(self, timeout=5.0):
        start = time.perf_counter()
        while True:
            if self.state()[0] == 0:
                self.set_state(1, os.getpid())
                if self.state() == (1, os.getpid()):
                    return True
            if time.perf_counter() - start > timeout:
                return False
            time.sleep(0.0002)

    def release(self):
        self.set_state(0, 0)

    def close(self):
        self._mm.close()
        os.close(self._fd)


class RefRing:
    """Same file format and the same whole-header rewrites as Member 1's MmapRingBuffer."""

    def __init__(self, path, capacity=1024, create=False):
        self.capacity = capacity
        if create:
            with open(path, "wb") as f:
                f.write(b"\x00" * (HEADER_SIZE + capacity * SLOT))
        self._fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._mm = mmap.mmap(self._fd, 0, access=mmap.ACCESS_WRITE)
        if create:
            HEADER.pack_into(self._mm, 0, 0, 0, capacity, SLOT, 0)

    def header(self):
        return HEADER.unpack_from(self._mm, 0)

    def raw_header(self):
        return bytes(self._mm[:HEADER_SIZE])

    def write_batch(self, packets):
        head, tail, cap, size, drops = self.header()
        occupied = head - tail if head >= tail else cap - (tail - head)
        free = (cap - 1) - occupied
        to_write = min(len(packets), free)
        dropped = len(packets) - to_write
        cur = head
        for i in range(to_write):
            off = HEADER_SIZE + cur * SLOT
            self._mm[off:off + SLOT] = packets[i]
            cur = (cur + 1) % cap
        HEADER.pack_into(self._mm, 0, cur, tail, cap, size, drops + dropped)     # whole header, like Member 1
        return to_write

    def close(self):
        self._mm.close()
        os.close(self._fd)


class RingTestCase(unittest.TestCase):
    capacity = 1024

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chronos_ring_test_")
        self.ring_path = os.path.join(self.dir, "ring.bin")
        self.lock_path = os.path.join(self.dir, "ring.lock")
        self.ring = RefRing(self.ring_path, self.capacity, create=True)
        self.lock = RefLock(self.lock_path, create=True)
        self.book = OrderBook()
        self.consumer = RingConsumer(self.ring_path, self.lock_path)

    def tearDown(self):
        self.consumer.close()
        self.ring.close()
        self.lock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestBasics(RingTestCase):
    def test_reads_layout_from_the_file_header(self):
        self.assertEqual((self.consumer.capacity, self.consumer.slot_size), (self.capacity, 29))
        self.assertEqual(self.consumer.pending(), 0)

    def test_empty_ring(self):
        r = self.consumer.drain_into(self.book)
        self.assertEqual((r["messages"], r["submitted"], r["ring_left"]), (0, 0, 0))

    def test_orders_in_the_ring_reach_the_book_and_tail_advances(self):
        self.ring.write_batch([pack(1, B, 99.5, 10), pack(2, S, 100.5, 20)])
        self.assertEqual(self.consumer.pending(), 2)
        r = self.consumer.drain_into(self.book)
        self.assertEqual((r["messages"], r["submitted"], r["rejected"]), (2, 2, 0))
        self.assertEqual((self.book.best_bid(), self.book.best_ask()), ((99.5, 10), (100.5, 20)))
        self.assertEqual(self.consumer.pending(), 0)
        self.assertEqual(self.ring.header()[:2], (2, 2))
        self.assertTrue(self.book.check_integrity())

    def test_crossing_orders_trade(self):
        self.ring.write_batch([pack(1, S, 100.0, 50), pack(2, B, 101.0, 80)])
        r = self.consumer.drain_into(self.book, collect=True)
        self.assertEqual(r["fills"], [(2, 1, 100.0, 50)])
        self.assertEqual((r["fill_count"], r["filled_quantity"]), (1, 50))
        self.assertEqual(self.book.best_bid(), (101.0, 30))

    def test_max_orders_limits_a_drain(self):
        self.ring.write_batch([pack(i, B, 50.0 + i * 0.01, 1) for i in range(1, 11)])
        r1 = self.consumer.drain_into(self.book, max_orders=4)
        self.assertEqual((r1["messages"], r1["ring_left"]), (4, 6))
        r2 = self.consumer.drain_into(self.book)
        self.assertEqual((r2["messages"], r2["ring_left"]), (6, 0))
        self.assertEqual(len(self.book), 10)

    def test_drain_all(self):
        self.ring.write_batch([pack(i, B, 50.0, 1) for i in range(1, 501)])
        total = self.consumer.drain_all(self.book)
        self.assertEqual((total["messages"], total["submitted"]), (500, 500))
        self.assertEqual(self.consumer.consumed_total, 500)


class TestCancelsAndRejects(RingTestCase):
    def test_cancel_message_removes_the_order(self):
        self.ring.write_batch([pack(1, B, 100.0, 10), pack(2, B, 100.0, 5), pack(1, C, 100.0, 0)])
        r = self.consumer.drain_into(self.book)
        self.assertEqual((r["submitted"], r["cancels"], r["cancels_missed"]), (2, 1, 0))
        self.assertEqual(self.book.level_orders(B, 100.0), [(2, 5)])

    def test_cancel_of_unknown_or_already_filled_order_is_counted_not_fatal(self):
        self.ring.write_batch([pack(1, S, 100.0, 10), pack(2, B, 100.0, 10),   # order 1 fully filled
                               pack(1, C, 100.0, 0), pack(999, C, 100.0, 0)])
        r = self.consumer.drain_into(self.book)
        self.assertEqual((r["cancels"], r["cancels_missed"]), (0, 2))
        self.assertEqual(len(self.book), 0)

    def test_bad_messages_are_rejected_and_the_rest_still_processed(self):
        self.ring.write_batch([
            pack(1, b"X", 100.0, 10),      # unknown side
            pack(2, B, 100.0, 0),          # zero quantity
            pack(3, B, -1.0, 5),           # bad price
            pack(4, B, 100.005, 5),        # off the tick grid
            pack(5, B, 100.0, 7),          # fine
            pack(5, S, 120.0, 7),          # duplicate id of a resting order
        ])
        r = self.consumer.drain_into(self.book)
        self.assertEqual((r["messages"], r["submitted"], r["rejected"]), (6, 1, 5))
        self.assertEqual(self.book.best_bid(), (100.0, 7))


class TestRingMechanics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chronos_ring_test_")
        self.ring_path = os.path.join(self.dir, "ring.bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_wraparound_keeps_order_over_many_cycles(self):
        ring = RefRing(self.ring_path, 64, create=True)
        book = OrderBook()
        with RingConsumer(self.ring_path, None, max_batch=50) as consumer:
            next_id = 1
            for cycle in range(400):                       # 400 x 37 messages through a 64-slot ring
                batch = [pack(next_id + i, B, 50.0, 1) for i in range(37)]
                self.assertEqual(ring.write_batch(batch), 37)
                next_id += 37
                consumer.drain_all(book)
        self.assertEqual(len(book), 400 * 37)
        ids = [o[0] for o in book.level_orders(B, 50.0)]
        self.assertEqual(ids, list(range(1, 400 * 37 + 1)))        # no loss, no duplicate, in order
        ring.close()

    def test_a_drain_that_crosses_the_end_of_the_ring(self):
        ring = RefRing(self.ring_path, 8, create=True)
        book = OrderBook()
        with RingConsumer(self.ring_path, None) as consumer:
            ring.write_batch([pack(i, B, 50.0, 1) for i in range(1, 7)])
            consumer.drain_into(book)                       # tail is now 6
            ring.write_batch([pack(i, B, 50.0, 1) for i in range(7, 12)])   # wraps: slots 6,7,0,1,2
            r = consumer.drain_into(book)
            self.assertEqual(r["messages"], 5)
        self.assertEqual([o[0] for o in book.level_orders(B, 50.0)], list(range(1, 12)))
        ring.close()

    def test_only_the_tail_field_is_written(self):
        ring = RefRing(self.ring_path, 128, create=True)
        ring.write_batch([pack(i, B, 50.0, 1) for i in range(1, 21)])
        before = ring.raw_header()
        with RingConsumer(self.ring_path, None) as consumer:
            consumer.drain_into(OrderBook())
        after = ring.raw_header()
        self.assertEqual(before[:4], after[:4])             # head
        self.assertEqual(before[8:], after[8:])             # capacity, slot size, drops
        self.assertEqual(struct.unpack_from("<I", after, 4)[0], 20)    # tail
        ring.close()

    def test_producer_overrun_counter_is_reported(self):
        ring = RefRing(self.ring_path, 16, create=True)
        written = ring.write_batch([pack(i, B, 50.0, 1) for i in range(1, 41)])   # only 15 fit
        self.assertEqual(written, 15)
        with RingConsumer(self.ring_path, None) as consumer:
            r = consumer.drain_into(OrderBook())
            self.assertEqual((r["messages"], r["ring_drops"], consumer.producer_drops()), (15, 25, 25))
        ring.close()

    def test_corrupt_header_is_detected(self):
        ring = RefRing(self.ring_path, 16, create=True)
        struct.pack_into("<I", ring._mm, 0, 9999)                     # head far outside the capacity
        with RingConsumer(self.ring_path, None) as consumer:
            with self.assertRaises(ValueError):
                consumer.drain_into(OrderBook())
            with self.assertRaises(ValueError):
                consumer.pending()
        ring.close()

    def test_wrong_slot_size_or_missing_file_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            RingConsumer(os.path.join(self.dir, "nope.bin"))
        ring = RefRing(self.ring_path, 16, create=True)
        struct.pack_into("<I", ring._mm, 12, 32)                      # slot size 32, not 29
        with self.assertRaises(ValueError):
            RingConsumer(self.ring_path)
        ring.close()

    def test_closed_consumer_refuses_work(self):
        ring = RefRing(self.ring_path, 16, create=True)
        consumer = RingConsumer(self.ring_path)
        consumer.close()
        consumer.close()                                              # second close is harmless
        with self.assertRaises(ValueError):
            consumer.drain_into(OrderBook())
        ring.close()


class TestDifferentialAgainstSubmit(RingTestCase):
    capacity = 512

    def test_ring_path_equals_the_python_path_for_random_traffic(self):
        rng = random.Random(9)
        messages, live = [], []
        for oid in range(1, 6001):
            if live and rng.random() < 0.2:
                messages.append((live.pop(rng.randrange(len(live))), C, 100.0, 0))
            else:
                live.append(oid)
                messages.append((oid, rng.choice((B, S)), 100 + rng.randint(-8, 8) * 0.25, rng.randint(1, 80)))
        # reference: plain Python calls, one order at a time
        ref = OrderBook()
        ref_fills = []
        for oid, side, price, qty in messages:
            if side == C:
                ref.cancel(oid)
            else:
                ref_fills.extend(ref.submit(oid, side, price, qty))
        # ring path: written in chunks through a small ring, drained between writes
        got_fills = []
        pos = 0
        while pos < len(messages):
            chunk = messages[pos:pos + 200]
            written = self.ring.write_batch([pack(*m) for m in chunk])
            pos += written
            got_fills.extend(self.consumer.drain_into(self.book, collect=True)["fills"])
        got_fills.extend(self.consumer.drain_into(self.book, collect=True)["fills"])
        self.assertEqual(got_fills, ref_fills)
        for side in (B, S):
            self.assertEqual(self.book.depth(side, 1000), ref.depth(side, 1000))
        self.assertTrue(self.book.check_integrity())


class TestNoGilAndLatency(RingTestCase):
    def test_matching_runs_without_the_gil(self):
        self.assertEqual(self.consumer.gil_held_in_last_drain, -1)
        self.ring.write_batch([pack(1, B, 50.0, 1)])
        self.consumer.drain_into(self.book)
        self.assertEqual(self.consumer.gil_held_in_last_drain, 0)

    def test_latency_is_measured_from_the_orders_own_timestamp(self):
        t0 = time.perf_counter_ns()
        self.ring.write_batch([pack(i, B, 50.0, 1, t0) for i in range(1, 6)])
        time.sleep(0.01)
        r = self.consumer.drain_into(self.book)
        self.assertGreaterEqual(r["latency_min_ns"], 9_000_000)
        self.assertLessEqual(r["latency_min_ns"], r["latency_max_ns"])
        self.assertEqual(r["latency_total_ns"] // 5 >= r["latency_min_ns"], True)

    def test_future_timestamps_give_zero_latency_not_a_huge_number(self):
        self.ring.write_batch([pack(1, B, 50.0, 1, 2**62)])
        self.assertEqual(self.consumer.drain_into(self.book)["latency_max_ns"], 0)


class TestLocking(RingTestCase):
    def test_lock_is_released_after_every_drain(self):
        self.ring.write_batch([pack(1, B, 50.0, 1)])
        self.consumer.drain_into(self.book)
        self.assertEqual(self.lock.state(), (0, 0))

    def test_lock_is_released_even_when_the_header_is_corrupt(self):
        struct.pack_into("<I", self.ring._mm, 0, 99999)
        with self.assertRaises(ValueError):
            self.consumer.drain_into(self.book)
        self.assertEqual(self.lock.state(), (0, 0))

    def test_times_out_when_a_live_process_holds_the_lock(self):
        consumer = RingConsumer(self.ring_path, self.lock_path, spin_timeout_sec=0.05)
        self.lock.set_state(1, os.getppid() or os.getpid())      # held by a process that is alive
        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            consumer.drain_into(self.book)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(self.lock.state()[0], 1)                # we did not steal a live lock
        consumer.close()

    def test_stale_lock_of_a_dead_process_is_taken_over(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.lock.set_state(1, proc.pid)
        self.ring.write_batch([pack(1, B, 50.0, 1)])
        r = self.consumer.drain_into(self.book)
        self.assertEqual(r["messages"], 1)
        self.assertEqual(self.lock.state(), (0, 0))


class ProcessHandle:
    """Wraps a producer started as a separate Python process (a subprocess running this file)."""

    def __init__(self, ring_path, lock_path, n):
        env = dict(os.environ)
        engine_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = engine_dir + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--produce", ring_path, lock_path, str(n)], env=env)

    def is_alive(self):
        return self.proc.poll() is None

    def join(self, timeout=None):
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class TestConcurrentProducer(unittest.TestCase):
    """A real producer running at the same time must not lose, repeat or reorder a message."""

    N = 30000

    def _run(self, start_producer):
        d = tempfile.mkdtemp(prefix="chronos_ring_test_")
        try:
            ring_path, lock_path = os.path.join(d, "r.bin"), os.path.join(d, "r.lock")
            RefRing(ring_path, 256, create=True).close()
            RefLock(lock_path, create=True).close()
            book = OrderBook()
            with RingConsumer(ring_path, lock_path, spin_timeout_sec=10.0) as consumer:
                worker = start_producer(ring_path, lock_path, self.N)
                deadline = time.time() + 60
                while consumer.consumed_total < self.N and time.time() < deadline:
                    consumer.drain_into(book)
                    if not worker.is_alive() and consumer.pending() == 0:
                        consumer.drain_into(book)          # producer finished or died: one last look
                        break
                worker.join(30)
            self.assertEqual(consumer.consumed_total, self.N, "producer did not deliver every message")
            ids = [o[0] for o in book.level_orders(B, 50.0)]
            self.assertEqual(ids, list(range(1, self.N + 1)))
            self.assertTrue(book.check_integrity())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_producer_thread(self):
        def start(ring_path, lock_path, n):
            t = threading.Thread(target=produce, args=(ring_path, lock_path, n))
            t.start()
            return t
        self._run(start)

    def test_producer_process(self):
        self._run(ProcessHandle)


def produce(ring_path, lock_path, n):
    """Producer loop (module level so a spawned process can import it)."""
    ring, lock = RefRing(ring_path, create=False), RefLock(lock_path)
    next_id = 1
    while next_id <= n:
        batch = [pack(i, B, 50.0, 1) for i in range(next_id, min(next_id + 50, n + 1))]
        if not lock.acquire(timeout=30):
            raise RuntimeError("producer could not get the lock")
        try:
            written = ring.write_batch(batch)
            # a full ring drops the rest of the batch (Member 1's protocol): resend only what was dropped
            if written < len(batch):
                head, tail, cap, size, drops = ring.header()
                HEADER.pack_into(ring._mm, 0, head, tail, cap, size, drops - (len(batch) - written))
        finally:
            lock.release()
        next_id += written
        if written == 0:
            time.sleep(0.0005)
    ring.close()
    lock.close()


def _find_member1_modules():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    if os.path.exists(os.path.join(root, "src", "ring_buffer.py")) and \
            os.path.exists(os.path.join(root, "src", "ipc_sync.py")):
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            return (importlib.import_module("src.ring_buffer"), importlib.import_module("src.ipc_sync"))
        except Exception:
            return None
    return None


MEMBER1 = _find_member1_modules()


@unittest.skipIf(MEMBER1 is None, "Member 1's src/ring_buffer.py and src/ipc_sync.py not found next to engine/")
class TestWithMember1sRealClasses(unittest.TestCase):
    def test_consumer_reads_what_member1s_ring_buffer_wrote(self):
        ring_mod, sync_mod = MEMBER1
        d = tempfile.mkdtemp(prefix="chronos_ring_test_")
        try:
            ring_path, lock_path = os.path.join(d, "r.bin"), os.path.join(d, "r.lock")
            ring = ring_mod.MmapRingBuffer(ring_path, capacity=2048, create=True)
            lock = sync_mod.MmapProcessLock(lock_path, create=True)
            book = OrderBook()
            with RingConsumer(ring_path, lock_path) as consumer:
                self.assertEqual((consumer.capacity, consumer.slot_size), (2048, 29))
                rng = random.Random(1)
                sent = 0
                for round_no in range(20):
                    packets = [pack(sent + i + 1, rng.choice((B, S)), 100 + rng.randint(-5, 5) * 0.5, rng.randint(1, 50), time.perf_counter_ns())
                               for i in range(200)]
                    with lock:
                        self.assertEqual(ring.write_batch(packets), 200)
                    sent += 200
                    r = consumer.drain_into(book)
                    self.assertEqual(r["messages"], 200)
                self.assertEqual(consumer.consumed_total, sent)
                self.assertEqual(ring.get_metrics()["occupied"], 0)       # Member 1's view agrees: ring is empty
                self.assertTrue(ring.verify_integrity())
            self.assertTrue(book.check_integrity())
            ring.close()
            lock.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--produce":      # used by ProcessHandle
        produce(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        unittest.main()
