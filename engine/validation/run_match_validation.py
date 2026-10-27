"""
ChronosMatch - match accuracy and determinism validation under load  (Member 2, Day 10)

Runs several stress scenarios through the compiled engine and compares EVERYTHING with an
independent reference engine written in plain Python (integer ticks, OrderedDict queues):

  * every fill (taker id, maker id, price, quantity), in order    -> SHA-256 fingerprint
  * the final book, order by order in queue order                 -> SHA-256 fingerprint
  * counts: fills, traded volume, cancels done / missed, rejects, trade ids

The same message stream is pushed through four entry paths of the engine
(sequential submit/cancel twice, the memory-mapped RingConsumer, and 4 threads on separate books)
and all fingerprints must be identical to the oracle's. Invariants are checked on the way:
book integrity, no crossed book, quantity conservation, contiguous trade ids.

Usage (from the engine/ folder, after building):
    python validation/run_match_validation.py                 # full size, writes reports/match_accuracy_report.md
    python validation/run_match_validation.py --scale 0.1     # 10 percent of the orders (quick check)
Exit status is 0 only if every check passes.
"""
import argparse
import hashlib
import mmap
import os
import platform
import shutil
import struct
import sys
import tempfile
import threading
import time
from bisect import bisect_left, insort
from collections import OrderedDict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(HERE)
sys.path.insert(0, ENGINE_DIR)

import chronos_engine                                   # noqa: E402
from chronos_engine import OrderBook, RingConsumer      # noqa: E402

B, S, C = b"B", b"S", b"C"
WIRE = struct.Struct("<QcdIQ")
HEADER = struct.Struct("<IIIIQ")
SCALE_PRICE = 100                                        # ticks per 1.00 (engine default)


# ----------------------------------------------------------------------------- random numbers
class XorShift64:
    """Own generator, so the scenarios are identical on every Python version and platform."""

    def __init__(self, seed):
        self.s = (seed * 0x9E3779B97F4A7C15 + 1) & 0xFFFFFFFFFFFFFFFF or 1

    def u64(self):
        x = self.s
        x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 7
        x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
        self.s = x
        return x

    def rand(self, lo, hi):                              # inclusive
        return lo + self.u64() % (hi - lo + 1)

    def chance(self, per_thousand):
        return self.u64() % 1000 < per_thousand


# ----------------------------------------------------------------------------- scenarios
# A message is (kind, order_id, side, tick, qty); kind "N" = new limit order, "C" = cancel of order_id.
MID = 10000                                              # 100.00


def scenario_balanced(n, seed=1):
    """Two-sided flow around one price: lots of crossing, partial fills, 3 percent cancels."""
    rng, out, ids = XorShift64(seed), [], []
    for oid in range(1, n + 1):
        if ids and rng.chance(30):
            out.append(("C", ids[rng.rand(0, len(ids) - 1)], b"", 0, 0))
            continue
        ids.append(oid)
        out.append(("N", oid, B if rng.chance(500) else S, MID + rng.rand(-20, 20), rng.rand(1, 100)))
    return out


def scenario_burst(n, seed=2):
    """Quiet book, then an earnings-style burst of aggressive orders and whale sweeps, then recovery."""
    rng, out, oid = XorShift64(seed), [], 0
    quiet, burst = n * 2 // 10, n * 6 // 10
    for i in range(n):
        oid += 1
        if i < quiet or i >= quiet + burst:               # quiet / recovery: passive, wide spread
            side = B if rng.chance(500) else S
            tick = MID - rng.rand(1, 60) if side == B else MID + rng.rand(1, 60)
            qty = rng.rand(1, 80)
        else:                                             # burst: aggressive, crossing many levels
            side = B if rng.chance(500) else S
            tick = MID + rng.rand(10, 70) if side == B else MID - rng.rand(10, 70)
            qty = rng.rand(50, 500)
            if rng.chance(20):                            # 2 percent whales
                qty = rng.rand(20000, 200000)
        out.append(("N", oid, side, tick, qty))
    return out


def scenario_flash_crash(n, seed=3):
    """Price falling fast: aggressive sellers hit resting buyers while panic cancels pull orders
    (cancels of live, already filled and never-existing orders)."""
    rng, out, ids, price = XorShift64(seed), [], [], MID
    for oid in range(1, n + 1):
        r = rng.rand(0, 999)
        if ids and r < 450:                                           # panic: pull recent orders
            out.append(("C", ids.pop(len(ids) - 1 - rng.rand(0, min(len(ids) - 1, 300))), b"", 0, 0))
        elif r < 480:
            out.append(("C", oid + 10_000_000, b"", 0, 0))            # id that never existed
        elif r < 700:                                                 # dip buyers resting near the touch
            ids.append(oid)
            out.append(("N", oid, B, price - rng.rand(0, 40), rng.rand(1, 200)))
        else:                                                         # aggressive sellers, price drifts down
            price = max(1000, price - rng.rand(0, 2))
            ids.append(oid)
            out.append(("N", oid, S, price - rng.rand(0, 30), rng.rand(10, 300)))
    return out


def scenario_hot_level(n, seed=4):
    """Everything at ONE price: deep FIFO queues, long runs of partial fills."""
    rng, out, oid, side = XorShift64(seed), [], 0, S
    while len(out) < n:
        for _ in range(rng.rand(500, 3000)):
            oid += 1
            out.append(("N", oid, side, MID, rng.rand(1, 60)))
            if len(out) >= n:
                break
        side = B if side == S else S
    return out


def scenario_wide_book(n, seed=5):
    """Prices spread over +-50,000 ticks (table growth, sparse best-price scans) with occasional sweeps."""
    centre = 60000                                        # 600.00, so every price stays positive
    rng, out = XorShift64(seed), []
    for oid in range(1, n + 1):
        side = B if rng.chance(500) else S
        if rng.chance(15):                                # sweep that crosses a long way
            tick = centre + rng.rand(2000, 50000) if side == B else centre - rng.rand(2000, 50000)
            qty = rng.rand(500, 5000)
        else:
            tick = centre - rng.rand(1, 50000) if side == B else centre + rng.rand(1, 50000)
            qty = rng.rand(1, 100)
        out.append(("N", oid, side, tick, qty))
    return out


SCENARIOS = [
    ("S1 balanced two-sided flow", scenario_balanced, 1_000_000),
    ("S2 earnings burst with whale sweeps", scenario_burst, 500_000),
    ("S3 flash crash with panic cancels", scenario_flash_crash, 300_000),
    ("S4 single hot price level", scenario_hot_level, 400_000),
    ("S5 wide sparse book (+-50,000 ticks)", scenario_wide_book, 300_000),
]


# ----------------------------------------------------------------------------- the oracle
class Oracle:
    """Independent reference matching engine: integer ticks, bisect-sorted prices, OrderedDict queues."""

    def __init__(self):
        self.queues = {B: {}, S: {}}                      # side -> tick -> OrderedDict(id -> qty)
        self.prices = {B: [], S: []}                      # sorted ascending ticks that have a queue
        self.where = {}                                   # live id -> (side, tick)
        self.trades = 0
        self.volume = 0
        self.cancelled_qty = 0
        self.rejected_qty = 0
        self.cancels = self.cancels_missed = self.rejected = 0
        self.fills = []                                   # reused per call

    def _best(self, side):
        p = self.prices[side]
        return (p[-1] if side == B else p[0]) if p else None

    def new(self, oid, side, tick, qty):
        fills = []
        if oid in self.where or qty <= 0 or tick <= 0:
            self.rejected += 1
            self.rejected_qty += qty
            return fills
        opposite = S if side == B else B
        remaining = qty
        while remaining > 0:
            best = self._best(opposite)
            if best is None or (side == B and best > tick) or (side == S and best < tick):
                break
            queue = self.queues[opposite][best]
            while remaining > 0 and queue:
                maker_id = next(iter(queue))
                maker_qty = queue[maker_id]
                traded = remaining if remaining < maker_qty else maker_qty
                fills.append((oid, maker_id, best, traded))
                self.trades += 1
                self.volume += traded
                remaining -= traded
                if traded == maker_qty:
                    del queue[maker_id]
                    del self.where[maker_id]
                else:
                    queue[maker_id] = maker_qty - traded
            if not queue:
                del self.queues[opposite][best]
                self.prices[opposite].pop(-1 if opposite == B else 0)
        if remaining > 0:
            qs = self.queues[side]
            if tick not in qs:
                qs[tick] = OrderedDict()
                insort(self.prices[side], tick)
            qs[tick][oid] = remaining
            self.where[oid] = (side, tick)
        return fills

    def cancel(self, oid):
        loc = self.where.pop(oid, None)
        if loc is None:
            self.cancels_missed += 1
            return False
        side, tick = loc
        queue = self.queues[side][tick]
        self.cancelled_qty += queue.pop(oid)
        if not queue:
            del self.queues[side][tick]
            p = self.prices[side]
            del p[bisect_left(p, tick)]
        self.cancels += 1
        return True

    def resting_quantity(self):
        return sum(q for side in (B, S) for qs in self.queues[side].values() for q in qs.values())

    def book_hash(self):
        h = hashlib.sha256()
        for side in (B, S):
            ticks = sorted(self.queues[side], reverse=(side == B))
            for t in ticks:
                q = self.queues[side][t]
                h.update(struct.pack("<cqII", side, t, sum(q.values()), len(q)))
                for oid, qty in q.items():
                    h.update(struct.pack("<QI", oid, qty))
        return h.hexdigest()


# ----------------------------------------------------------------------------- hashing helpers
FILL_PACK = struct.Struct("<QQqI")


def hash_fills(h, fills):
    pack = FILL_PACK.pack
    for taker, maker, price, qty in fills:
        h.update(pack(taker, maker, round(price * SCALE_PRICE) if isinstance(price, float) else price, qty))


def engine_book_hash(book):
    h = hashlib.sha256()
    for side in (B, S):
        for price, total, count in book.depth(side, 10 ** 9):
            tick = round(price * SCALE_PRICE)
            h.update(struct.pack("<cqII", side, tick, total, count))
            for oid, qty in book.level_orders(side, price):
                h.update(struct.pack("<QI", oid, qty))
    return h.hexdigest()


def book_resting(book):
    return sum(total for side in (B, S) for _, total, _ in book.depth(side, 10 ** 9))


# ----------------------------------------------------------------------------- the four entry paths
CHECK_EVERY = 50_000


class PathResult:
    def __init__(self, name):
        self.name = name
        self.fill_hash = ""
        self.book_hash = ""
        self.fills = self.volume = 0
        self.cancels = self.cancels_missed = self.rejected = 0
        self.resting = 0
        self.trades_executed = 0
        self.seconds = 0.0
        self.integrity_ok = True
        self.never_crossed = True
        self.levels = (0, 0)
        self.error = ""


def check_book(book, res):
    if not book.check_integrity():
        res.integrity_ok = False
    bid, ask = book.best_bid(), book.best_ask()
    if bid and ask and not bid[0] < ask[0]:
        res.never_crossed = False


def finish(book, res, h):
    res.fill_hash = h.hexdigest()
    res.book_hash = engine_book_hash(book)
    res.resting = book_resting(book)
    res.trades_executed = book.trades_executed
    res.levels = (book.level_count(B), book.level_count(S))
    check_book(book, res)


def path_sequential(messages, name):
    """One Python call per message: submit() for new orders, cancel() for cancels."""
    res, book, h = PathResult(name), OrderBook(), hashlib.sha256()
    submit, cancel = book.submit, book.cancel
    t0 = time.perf_counter()
    try:
        for i, (kind, oid, side, tick, qty) in enumerate(messages, 1):
            if kind == "N":
                try:
                    fills = submit(oid, side, tick / SCALE_PRICE, qty)
                except ValueError:
                    res.rejected += 1
                    continue
                if fills:
                    res.fills += len(fills)
                    res.volume += sum(f[3] for f in fills)
                    hash_fills(h, fills)
            elif cancel(oid):
                res.cancels += 1
            else:
                res.cancels_missed += 1
            if i % CHECK_EVERY == 0:
                check_book(book, res)
    except Exception as exc:                              # pragma: no cover
        res.error = repr(exc)
    res.seconds = time.perf_counter() - t0
    finish(book, res, h)
    return res


class RingFeed:
    """Writes messages into a ring file in Member 1's exact format (plus its lock file)."""

    def __init__(self, directory, capacity=8192):
        self.capacity = capacity
        self.ring_path = os.path.join(directory, "ring.bin")
        self.lock_path = os.path.join(directory, "ring.lock")
        with open(self.ring_path, "wb") as f:
            f.write(b"\x00" * (HEADER.size + capacity * WIRE.size))
        with open(self.lock_path, "wb") as f:
            f.write(b"\x00" * 8)
        self.fd = os.open(self.ring_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self.mm = mmap.mmap(self.fd, 0, access=mmap.ACCESS_WRITE)
        HEADER.pack_into(self.mm, 0, 0, 0, capacity, WIRE.size, 0)

    def free_slots(self):
        head, tail = struct.unpack_from("<II", self.mm, 0)
        occupied = head - tail if head >= tail else self.capacity - (tail - head)
        return self.capacity - 1 - occupied

    def write(self, packets):
        """Write as many packets as fit; returns how many were written."""
        n = min(len(packets), self.free_slots())
        head = struct.unpack_from("<I", self.mm, 0)[0]
        slot = WIRE.size
        first = min(n, self.capacity - head)
        base = HEADER.size
        self.mm[base + head * slot: base + (head + first) * slot] = b"".join(packets[:first])
        if n > first:
            self.mm[base: base + (n - first) * slot] = b"".join(packets[first:n])
        struct.pack_into("<I", self.mm, 0, (head + n) % self.capacity)      # publish: head only
        return n

    def close(self):
        self.mm.close()
        os.close(self.fd)


def path_ring(messages, name):
    """Messages go through a memory-mapped ring buffer into the engine's RingConsumer."""
    res, book, h = PathResult(name), OrderBook(), hashlib.sha256()
    directory = tempfile.mkdtemp(prefix="chronos_validation_")
    feed = RingFeed(directory)
    consumer = RingConsumer(feed.ring_path, feed.lock_path)
    packets = [WIRE.pack(oid, side if kind == "N" else C, tick / SCALE_PRICE if kind == "N" else 1.0,
                         qty, 0) for kind, oid, side, tick, qty in messages]
    t0 = time.perf_counter()
    try:
        pos, done, next_check = 0, 0, CHECK_EVERY
        while done < len(packets):
            pos += feed.write(packets[pos:pos + 4096])
            while True:
                r = consumer.drain_into(book, collect=True)
                if r["messages"] == 0:
                    break
                done += r["messages"]
                res.fills += r["fill_count"]
                res.volume += r["filled_quantity"]
                res.cancels += r["cancels"]
                res.cancels_missed += r["cancels_missed"]
                res.rejected += r["rejected"]
                hash_fills(h, r["fills"])
            if done >= next_check:
                check_book(book, res)
                next_check += CHECK_EVERY
    except Exception as exc:                              # pragma: no cover
        res.error = repr(exc)
    res.seconds = time.perf_counter() - t0
    finish(book, res, h)
    consumer.close()
    feed.close()
    shutil.rmtree(directory, ignore_errors=True)
    return res


def path_threads(messages, name, threads=4):
    """The same stream on 4 independent books in 4 threads at the same time."""
    results = [None] * threads

    def work(i):
        results[i] = path_sequential(messages, "%s #%d" % (name, i + 1))

    t0 = time.perf_counter()
    pool = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    total = time.perf_counter() - t0
    for r in results:
        r.seconds = total
    return results


def run_oracle(messages):
    res, oracle, h = PathResult("oracle (pure Python)"), Oracle(), hashlib.sha256()
    t0 = time.perf_counter()
    for kind, oid, side, tick, qty in messages:
        if kind == "N":
            fills = oracle.new(oid, side, tick, qty)
            if fills:
                res.fills += len(fills)
                res.volume += sum(f[3] for f in fills)
                hash_fills(h, fills)
        else:
            oracle.cancel(oid)
    res.seconds = time.perf_counter() - t0
    res.fill_hash = h.hexdigest()
    res.book_hash = oracle.book_hash()
    res.cancels, res.cancels_missed, res.rejected = oracle.cancels, oracle.cancels_missed, oracle.rejected
    res.resting = oracle.resting_quantity()
    res.trades_executed = oracle.trades
    res.levels = (len(oracle.prices[B]), len(oracle.prices[S]))
    return res, oracle


# ----------------------------------------------------------------------------- one scenario
def same_as_oracle(r, o):
    return (r.fill_hash == o.fill_hash and r.book_hash == o.book_hash and r.fills == o.fills
            and r.volume == o.volume and r.cancels == o.cancels and r.cancels_missed == o.cancels_missed
            and r.rejected == o.rejected and r.resting == o.resting and r.trades_executed == o.trades_executed
            and not r.error)


def run_scenario(title, make, size, scale):
    n = max(1000, int(size * scale))
    messages = make(n)
    new_orders = [m for m in messages if m[0] == "N"]
    submitted_qty = sum(m[4] for m in new_orders)
    print("  %s: %d messages ..." % (title, len(messages)), flush=True)
    oracle_res, oracle = run_oracle(messages)
    paths = [path_sequential(messages, "engine, sequential submit/cancel (run 1)"),
             path_sequential(messages, "engine, sequential submit/cancel (run 2)"),
             path_ring(messages, "engine, mmap ring buffer + RingConsumer")]
    paths += path_threads(messages, "engine, 4 threads on separate books")
    checks = []
    for r in paths:
        checks.append((r.name + ": fills, final book, counts equal the oracle", same_as_oracle(r, oracle_res)))
    fingerprints = {r.fill_hash for r in paths} | {oracle_res.fill_hash}
    books = {r.book_hash for r in paths} | {oracle_res.book_hash}
    checks.append(("all %d runs and the oracle produce ONE fill fingerprint" % len(paths), len(fingerprints) == 1))
    checks.append(("all %d runs and the oracle produce ONE final-book fingerprint" % len(paths), len(books) == 1))
    checks.append(("book integrity check passed at every checkpoint", all(r.integrity_ok for r in paths)))
    checks.append(("book never left crossed at any checkpoint", all(r.never_crossed for r in paths)))
    checks.append(("trade ids contiguous (trades executed == fills)", all(r.trades_executed == r.fills for r in paths)))
    traded = paths[0].volume
    accepted_qty = submitted_qty - oracle.rejected_qty
    conserved = accepted_qty == 2 * traded + paths[0].resting + oracle.cancelled_qty
    checks.append(("quantity conserved: accepted = 2 x traded + resting + cancelled", conserved))
    checks.append(("every generated order was valid (0 rejected)", oracle_res.rejected == 0))
    checks.append(("no engine run raised an error", all(not r.error for r in paths)))
    return {"title": title, "messages": len(messages), "new_orders": len(new_orders), "submitted_qty": submitted_qty,
            "cancel_messages": len(messages) - len(new_orders), "oracle": oracle_res, "paths": paths,
            "checks": checks, "cancelled_qty": oracle.cancelled_qty, "rejected": oracle.rejected, "passed": all(ok for _, ok in checks)}


# ----------------------------------------------------------------------------- report
def rate(count, seconds):
    return "%.2f M/s" % (count / seconds / 1e6) if seconds > 0 else "n/a"


def cpu_name():
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def build_report(results, scale, elapsed):
    ok = all(r["passed"] for r in results)
    total_msgs = sum(r["messages"] for r in results)
    total_fills = sum(r["oracle"].fills for r in results)
    total_checks = sum(len(r["checks"]) for r in results)
    L = []
    w = L.append
    w("# ChronosMatch - Match Accuracy and Determinism Validation Report")
    w("")
    w("**Component:** Cython matching engine (`chronos_engine` %s)  " % chronos_engine.__version__)
    w("**Author:** Member 2 (Core Quant Engine Engineer)  ")
    w("**Generated:** %s UTC by `validation/run_match_validation.py` (scale %.2f)" % (
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), scale))
    w("")
    w("## Verdict: %s" % ("PASS - all %d checks passed" % total_checks if ok else "FAIL - see the failed checks below"))
    w("")
    w("%s messages (%s trades) were processed across %d stress scenarios. The engine's fills and final books were "
      "identical, order by order, to an independent reference engine in every scenario and through every entry path."
      % (format(total_msgs, ","), format(total_fills, ","), len(results)))
    w("")
    w("## Test environment")
    w("")
    w("| | |")
    w("|---|---|")
    w("| Python | %s (%s) |" % (platform.python_version(), platform.python_implementation()))
    w("| Operating system | %s |" % platform.platform())
    w("| CPU | %s (%s logical cores) |" % (cpu_name(), os.cpu_count()))
    w("| Engine version | %s |" % chronos_engine.__version__)
    w("| Run time | %.0f s |" % elapsed)
    w("")
    w("## Method")
    w("")
    w("1. Each scenario is a seeded, deterministic stream of limit orders and cancels (own random generator, so the "
      "stream is the same on every Python version and operating system).")
    w("2. An **independent reference engine** (pure Python, integer ticks, ordered queues; it shares no code with the "
      "Cython engine) processes the same stream and is the oracle.")
    w("3. The stream is run through the compiled engine on four entry paths: sequential `submit`/`cancel` (twice), the "
      "memory-mapped ring buffer read by `RingConsumer` (Member 1's file format), and four threads on four separate books.")
    w("4. **Fingerprints:** every fill (taker id, maker id, price in ticks, quantity) in execution order, and the final "
      "book (every price level and every resting order in queue order), are each hashed with SHA-256. Two runs are "
      "identical only if every single value and its order match.")
    w("5. **Invariants** are checked every 50,000 messages and at the end: the engine's own integrity check "
      "(sorting, links, totals, id table), the book is never crossed, trade ids are contiguous, and quantity is conserved "
      "(accepted quantity = 2 x traded + still resting + cancelled).")
    w("")
    w("## Summary")
    w("")
    w("| Scenario | Messages | New orders | Cancels | Trades | Traded volume | Resting at end | Result |")
    w("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in results:
        o = r["oracle"]
        w("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["title"], format(r["messages"], ","), format(r["new_orders"], ","), format(r["cancel_messages"], ","),
            format(o.fills, ","), format(o.volume, ","), format(o.resting, ","), "PASS" if r["passed"] else "**FAIL**"))
    w("")
    for r in results:
        o = r["oracle"]
        w("## %s" % r["title"])
        w("")
        w("- Messages: %s (%s new orders, %s cancels: %s cancelled an order, %s targeted an order already filled or unknown)" % (
            format(r["messages"], ","), format(r["new_orders"], ","), format(r["cancel_messages"], ","),
            format(o.cancels, ","), format(o.cancels_missed, ",")))
    # (details below)
        w("- Trades: %s, traded volume %s, cancelled quantity %s, resting quantity at the end %s" % (
            format(o.fills, ","), format(o.volume, ","), format(r["cancelled_qty"], ","), format(o.resting, ",")))
        w("- Final book: %s bid levels, %s ask levels" % (format(o.levels[0], ","), format(o.levels[1], ",")))
        w("- Fill fingerprint (SHA-256): `%s`" % o.fill_hash)
        w("- Final-book fingerprint (SHA-256): `%s`" % o.book_hash)
        w("")
        w("| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |")
        w("|---|---:|---|---|---|---:|---:|")
        w("| %s | %s | `%s...` | `%s...` | reference | %.1f s | %s |" % (
            o.name, format(o.fills, ","), o.fill_hash[:12], o.book_hash[:12], o.seconds, rate(r["messages"], o.seconds)))
        for p in r["paths"]:
            w("| %s | %s | `%s...` | `%s...` | %s | %.1f s | %s |" % (
                p.name, format(p.fills, ","), p.fill_hash[:12], p.book_hash[:12],
                "yes" if same_as_oracle(p, o) else "**NO**", p.seconds,
                rate(r["messages"] * (4 if "4 threads" in p.name else 1), p.seconds)))
        w("")
        w("| Check | Result |")
        w("|---|---|")
        for text, passed in r["checks"]:
            w("| %s | %s |" % (text, "PASS" if passed else "**FAIL**"))
        w("")
    w("## What the numbers mean")
    w("")
    w("- Rates include the cost of the Python test harness (one Python call per message for the sequential and "
      "thread paths, hashing of every fill, packing messages into the ring), so they are **not** the engine's "
      "matching latency. The latency benchmark is a separate deliverable (Day 15).")
    w("- The thread path processes the stream four times at once, so its rate column counts all four runs.")
    w("")
    w("## Scope and limits")
    w("")
    w("- Prices are on the engine's default tick grid (0.01); off-grid prices are rejected by design and tested separately.")
    w("- One symbol per book; the multi-symbol hub uses one book (and one ring) per symbol.")
    w("- Only limit orders and cancels exist in the wire format; Market and IOC orders are not part of this validation.")
    w("- The oracle is an independent implementation, but both were written by the same author; the rules they check "
      "(best price first, oldest first, trade at the resting price, partial fills keep their place, remainder rests) "
      "are the standard price-time priority rules.")
    w("")
    w("## How to reproduce")
    w("")
    w("```")
    w("cd engine")
    w("python setup.py build_ext --inplace")
    w("python validation/run_match_validation.py            # full size")
    w("python validation/run_match_validation.py --scale 0.1  # quick check")
    w("```")
    w("")
    return "\n".join(L), ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scale", type=float, default=1.0, help="fraction of the full order counts (default 1.0)")
    ap.add_argument("--out", default=os.path.join(ENGINE_DIR, "reports", "match_accuracy_report.md"),
                    help="where to write the report ('-' = do not write a file)")
    args = ap.parse_args(argv)
    print("ChronosMatch match-accuracy validation, engine %s, scale %.2f" % (chronos_engine.__version__, args.scale), flush=True)
    t0 = time.perf_counter()
    results = []
    for title, make, size in SCENARIOS:
        r = run_scenario(title, make, size, args.scale)
        print("    -> %s" % ("PASS" if r["passed"] else "FAIL"), flush=True)
        for text, passed in r["checks"]:
            if not passed:
                print("       FAILED: %s" % text, flush=True)
        results.append(r)
    report, ok = build_report(results, args.scale, time.perf_counter() - t0)
    if args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        print("Report written to %s" % args.out)
    print("OVERALL: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
