import argparse
import curses
import os
import random
import struct
import subprocess
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from collections import deque, namedtuple

VERSION = "0.8.0"

# Defaults. The launch flags below can override the first four at startup.
REFRESH_MS = 100       # UI refresh interval (about 10 frames/s)
LEVELS = 5             # price levels shown per side
TARGET_OPS = 100_000   # target throughput from the project plan
LATENCY_BUFFER = 5000  # rolling window: how many latency samples are kept
USE_COLOR = True       # flag: --no-color (also off if NO_COLOR is set)
WHALE_EVERY_S = 12.0   # mock only: average seconds between whale sweeps

# SLA: a trade breaches it when its latency is strictly greater than this.
SLA_NS = 50_000        # in nanoseconds (flag: --sla-us)
SLA_US = SLA_NS / 1000 # the same limit in microseconds
FLASH_S = 1.5          # how long the breach alert flashes after a breach
SHIFT_FLASH_S = 1.5    # how long a spread shift stays highlighted
WHALE_FLASH_S = 3.0    # how long a whale sweep alert flashes

TELEMETRY_S = 1.0      # how often CPU and memory are sampled
RING_SLOTS = 65_536    # mock shared-memory ring capacity, in event slots
RING_WARN = 0.80       # high watermark: alert when the ring is this full
RING_FLASH_S = 2.0     # how long the watermark alert stays lit

WORKER_SLEEP = 0.001   # engine consumer loop pause
TICK = 0.01            # price step between levels
MAX_QTY = 2000
FILL_BUFFER = 500      # how many recent events the UI keeps

MIN_H, MIN_W = 28, 70  # smallest terminal the layout fits in
WIDE_W = 130           # at this width everything is visible at once
TOP_COL_W = 46         # width of the order book and metrics panels (wide)
LOG_W = 74             # width of the trade log in the wide layout
PAGE = 10              # lines moved by PgUp / PgDn

HIST_MAX_BINS = 20     # most rows the histogram will use
HIST_LO_FLOOR_US = 0.1 # lowest edge of the log-scale histogram

# Percentiles shown in the Metrics panel, in permille (per 1000) so the
# maths stays in whole numbers: 999 = 99.9th percentile.
PCT_PERMILLE = (("p50", 500), ("p90", 900), ("p99", 990), ("p99.9", 999))

# Binary layout of one engine event (a fill, a cancel or a whale sweep).
# PLACEHOLDER: confirm the real layout with the IPC/engine members.
#   kind       0 = fill, 1 = cancel, 2 = whale sweep
#   levels     how many price levels the order swept (whale sweeps)
#   ts_ns      wall-clock time of the event (for display)
#   ingress_ns time.perf_counter_ns() stamped when the order entered the system
FILL_STRUCT = struct.Struct("<QBBBIdQQ")
RawFill = namedtuple(
    "RawFill", "order_id side kind levels qty price ts_ns ingress_ns"
)
# egress_ns is stamped by the UI reader when it decodes the event.
# latency_ns = egress_ns - ingress_ns (None if the clocks disagree).
Fill = namedtuple(
    "Fill",
    "order_id side kind levels qty price ts_ns ingress_ns egress_ns latency_ns",
)
SIDE_BUY, SIDE_SELL = 0, 1
KIND_FILL, KIND_CANCEL, KIND_WHALE = 0, 1, 2

# n = samples in the window, pct = {"p50": value or None, ...},
# ordered = all window samples in microseconds, sorted (feeds the histogram),
# over = how many window samples are above the SLA
LatencyStats = namedtuple("LatencyStats", "last avg min max n pct ordered over")
# fill_total counts every event (fills, cancels and whales); cancel_total and
# whale_total are parts of it. whale = (side, levels, qty, price) of the most
# recent whale sweep, whale_age = seconds since it happened.
# ring = None, or (capacity, used, peak_used, watermark_alerts, alert_age)
# describing the shared-memory ring buffer.
Snapshot = namedtuple(
    "Snapshot",
    "book rate peak total fills fill_total cancel_total whale_total whale "
    "whale_age latency polls clock_errors sla_checked sla_breaches breach_age "
    "ring",
)
# Resource use of this dashboard process (cpu = % of one core).
Health = namedtuple("Health", "cpu load cores rss_mb peak_mb threads uptime")

# Display styles, filled in by init_colors(). Without color support they
# fall back to plain attributes so the meaning is still visible.
COLORS = {"fill": 0, "cancel": 0, "spread": 0, "whale": 0}


# ---------------------------------------------------------------- CLI

def bounded_int(lo, hi):
    """argparse type: a whole number between lo and hi."""
    def check(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(
                f"must be between {lo} and {hi}, got {value}"
            )
        return value
    return check


def positive_float(text):
    """argparse type: a number greater than zero."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0, got {value}")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="ChronosMatch live latency dashboard (curses terminal UI).",
        epilog=(
            "examples:\n"
            "  python3 ui/dashboard.py\n"
            "  python3 ui/dashboard.py --levels 8 --refresh-ms 50\n"
            "  python3 ui/dashboard.py --sla-us 30 --seed 42 --duration 30\n"
            "  python3 ui/dashboard.py --whale-every 5\n"
            "  python3 ui/dashboard.py --no-color\n"
            "\n"
            "colors: green = fill, red = cancel, yellow = spread shift, "
            "magenta = whale sweep\n"
            "\n"
            "keys while running:\n"
            "  q quit | Up/Down scroll log | PgUp/PgDn page | f or End follow live\n"
            "  h cycle the bottom panel: log, histogram, system health "
            "(narrow terminals;\n"
            "    wide terminals show all of them at once)\n"
            "  w trigger a test whale sweep (mock source only)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--source", choices=["mock"], default="mock",
        help="data source (only 'mock' for now; the real engine is added "
             "at integration)",
    )
    parser.add_argument(
        "-r", "--refresh-ms", type=bounded_int(20, 1000), default=REFRESH_MS,
        metavar="MS", help=f"screen refresh interval in ms (default {REFRESH_MS})",
    )
    parser.add_argument(
        "-l", "--levels", type=bounded_int(1, 10), default=LEVELS,
        metavar="N", help=f"price levels per side in the ladder (default {LEVELS})",
    )
    parser.add_argument(
        "-t", "--target-ops", type=bounded_int(1, 10_000_000),
        default=TARGET_OPS, metavar="N",
        help=f"target orders/sec for the throughput bar (default {TARGET_OPS})",
    )
    parser.add_argument(
        "-w", "--window", type=bounded_int(100, 100_000),
        default=LATENCY_BUFFER, metavar="N",
        help="rolling window: how many recent latency samples the percentiles "
             f"and histogram use (default {LATENCY_BUFFER})",
    )
    parser.add_argument(
        "--sla-us", type=positive_float, default=SLA_US, metavar="US",
        help="SLA threshold in microseconds; a trade slower than this is "
             f"flagged as a breach (default {SLA_US:g})",
    )
    parser.add_argument(
        "--whale-every", type=positive_float, default=WHALE_EVERY_S,
        metavar="SECONDS",
        help="mock source: average seconds between whale sweeps "
             f"(default {WHALE_EVERY_S:g})",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="turn colors off (also off when the NO_COLOR variable is set)",
    )
    parser.add_argument(
        "-m", "--mid-price", type=positive_float, default=150.0, metavar="PRICE",
        help="starting price of the mock market (default 150.0)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="fixed random seed so a mock run is repeatable",
    )
    parser.add_argument(
        "-d", "--duration", type=positive_float, default=None, metavar="SECONDS",
        help="quit automatically after this many seconds (default: run until q)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}",
    )
    return parser


def apply_args(args):
    """Copy the launch options into the module settings the UI reads."""
    global REFRESH_MS, LEVELS, TARGET_OPS, LATENCY_BUFFER, SLA_NS, SLA_US
    global USE_COLOR, WHALE_EVERY_S
    REFRESH_MS = args.refresh_ms
    LEVELS = args.levels
    TARGET_OPS = args.target_ops
    LATENCY_BUFFER = args.window
    SLA_NS = int(round(args.sla_us * 1000))
    SLA_US = SLA_NS / 1000
    WHALE_EVERY_S = args.whale_every
    USE_COLOR = not args.no_color and not os.environ.get("NO_COLOR")
    if args.seed is not None:
        random.seed(args.seed)


# --------------------------------------------------------- mock engine

class MockOrderBook:
    """Fake multi-level book. Lives inside the mock stream below."""

    def __init__(self, mid=150.00):
        self.mid = mid
        self.spread_ticks = 1
        self.bids = []  # list of (price, qty), best first
        self.asks = []  # list of (price, qty), best first
        self.update()

    def update(self, dt=0.001):
        self.mid += random.uniform(-0.03, 0.03)
        # the spread changes about once every 1-2 seconds, so a spread
        # shift is a visible event and not a constant flicker
        if random.random() < dt * 0.7:
            self.spread_ticks = random.choice(
                [t for t in (1, 2, 3) if t != self.spread_ticks]
            )
        best_bid = round(self.mid, 2)
        best_ask = round(best_bid + self.spread_ticks * TICK, 2)
        self.bids = [
            (round(best_bid - i * TICK, 2), random.randint(100, MAX_QTY))
            for i in range(LEVELS)
        ]
        self.asks = [
            (round(best_ask + i * TICK, 2), random.randint(100, MAX_QTY))
            for i in range(LEVELS)
        ]

    @property
    def best_bid(self):
        return self.bids[0][0]

    @property
    def best_ask(self):
        return self.asks[0][0]

    @property
    def spread(self):
        return round(self.best_ask - self.best_bid, 2)


class BookSnapshot:
    """Frozen copy of the book, safe to draw while the worker keeps running."""

    def __init__(self, bids, asks):
        self.bids = bids
        self.asks = asks

    @property
    def best_bid(self):
        return self.bids[0][0]

    @property
    def best_ask(self):
        return self.asks[0][0]

    @property
    def spread(self):
        return round(self.best_ask - self.best_bid, 2)


class MockEngineStream:
    """Stands in for the engine output until the real one exists.

    Replace this class with the real stream. It needs:
      - read() -> (orders_processed: int, event_bytes: bytes)
        where every event carries the ingress perf_counter_ns() stamp
      - book   -> object with .bids and .asks (lists of (price, qty))
      - ring_stats() -> (capacity, used, peak_used) of the shared-memory
        ring buffer (optional: the health panel shows 'no data' without it)
    """

    def __init__(self, mid=150.00):
        self.book = MockOrderBook(mid)
        self.last = time.perf_counter()
        self.next_id = 1
        self.whale_requested = False
        self.next_whale_t = self.last + WHALE_EVERY_S * 0.5
        self.ring_floor = int(0.09 * RING_SLOTS)  # resting backlog
        self.ring_used = self.ring_floor
        self.ring_peak = self.ring_used

    def request_whale(self):
        """Ask for a test whale sweep (the 'w' key). Mock source only."""
        self.whale_requested = True

    def ring_stats(self):
        """(capacity, used, peak_used) of the mock shared-memory ring."""
        return RING_SLOTS, self.ring_used, self.ring_peak

    @staticmethod
    def _ingress_ns():
        """Backdate an ingress stamp by a mock engine latency (~25 us).
        Spikes: 2% are 3x slower (about 75 us, an SLA breach), about 0.2%
        are 10x slower."""
        latency_us = max(1.0, random.gauss(25, 4))
        if random.random() < 0.02:
            latency_us *= 3
        if random.random() < 0.002:
            latency_us *= 10
        return time.perf_counter_ns() - int(latency_us * 1000)

    def _whale_record(self):
        """One big order that clears several price levels of the book."""
        side = random.choice((SIDE_BUY, SIDE_SELL))
        levels = random.randint(min(3, LEVELS), min(6, LEVELS))
        # a buy sweeps the asks, a sell sweeps the bids
        book_side = self.book.asks if side == SIDE_BUY else self.book.bids
        qty = sum(q for _, q in book_side[:levels])  # all liquidity cleared
        price = book_side[levels - 1][0]             # deepest level reached
        order_id = self.next_id
        self.next_id += 1
        return FILL_STRUCT.pack(
            order_id, side, KIND_WHALE, levels, qty, price,
            time.time_ns(), self._ingress_ns(),
        )

    def read(self):
        now = time.perf_counter()
        dt = now - self.last
        self.last = now

        self.book.update(dt)
        n_orders = int(TARGET_OPS * random.uniform(0.85, 1.10) * dt)

        raw = b""
        if random.random() < dt * 25:  # about 25 events per second
            side = random.choice((SIDE_BUY, SIDE_SELL))
            qty = random.randint(10, 500)

            if self.next_id > 1 and random.random() < 0.15:
                # a cancel: refers to an earlier order, at its resting price
                kind = KIND_CANCEL
                order_id = random.randint(max(1, self.next_id - 200),
                                          self.next_id - 1)
                price = (self.book.bids[0][0] if side == SIDE_BUY
                         else self.book.asks[0][0])
            else:
                # a fill: trades against the opposite side of the book
                kind = KIND_FILL
                order_id = self.next_id
                self.next_id += 1
                price = (self.book.bids[0][0] if side == SIDE_SELL
                         else self.book.asks[0][0])

            raw += FILL_STRUCT.pack(
                order_id, side, kind, 0, qty, price,
                time.time_ns(), self._ingress_ns(),
            )

        if self.whale_requested or now >= self.next_whale_t:
            self.whale_requested = False
            self.next_whale_t = now + WHALE_EVERY_S * random.uniform(0.6, 1.4)
            raw += self._whale_record()
            # a whale is a burst: the ring buffer suddenly fills up
            self.ring_used = max(self.ring_used,
                                 int(RING_SLOTS * random.uniform(0.90, 0.98)))

        # the consumer drains the backlog back toward a small resting level
        self.ring_floor = int(min(max(
            self.ring_floor + random.uniform(-0.0015, 0.0015) * RING_SLOTS,
            0.06 * RING_SLOTS), 0.12 * RING_SLOTS))
        if self.ring_used > self.ring_floor:
            drain = (self.ring_used - self.ring_floor) * min(1.0, dt * 0.25)
            self.ring_used -= int(drain)
        else:
            self.ring_used = self.ring_floor
        self.ring_peak = max(self.ring_peak, self.ring_used)

        return n_orders, raw


def make_stream(args):
    """Create the data source chosen with --source."""
    if args.source == "mock":
        return MockEngineStream(args.mid_price)
    raise ValueError(f"unknown source: {args.source}")


class EngineReader:
    """Reads the engine stream, decodes events and stamps their egress time."""

    def __init__(self, stream):
        self.stream = stream
        self.fills = deque(maxlen=FILL_BUFFER)
        self.latencies = deque(maxlen=LATENCY_BUFFER)  # nanoseconds, rolling
        self.fill_total = 0         # all events: fills, cancels and whales
        self.cancel_total = 0       # of those, how many were cancels
        self.whale_total = 0        # of those, how many were whale sweeps
        self.last_whale = None      # (side, levels, qty, price)
        self.last_whale_t = None    # perf_counter() time of the latest whale
        self.clock_errors = 0
        self.sla_checked = 0        # events with a valid latency
        self.sla_breaches = 0       # of those, how many exceeded the SLA
        self.last_breach_t = None   # perf_counter() time of the latest breach
        self.ring_warns = 0         # times the ring crossed the high watermark
        self.ring_hot = False       # is the ring above the watermark right now
        self.ring_warn_t = None     # perf_counter() time it was last above it

    @property
    def book(self):
        return self.stream.book

    def _check_ring(self):
        """Count a watermark alert each time the ring fills past RING_WARN."""
        if not hasattr(self.stream, "ring_stats"):
            return
        cap, used, _peak = self.stream.ring_stats()
        hot = cap > 0 and used >= RING_WARN * cap
        if hot and not self.ring_hot:
            self.ring_warns += 1
        self.ring_hot = hot
        if hot:
            self.ring_warn_t = time.perf_counter()

    def poll(self):
        """Pull new data. Returns how many orders the engine processed."""
        n_orders, raw = self.stream.read()
        self._check_ring()
        size = FILL_STRUCT.size
        for off in range(0, len(raw) - size + 1, size):
            egress_ns = time.perf_counter_ns()  # egress timestamp
            rf = RawFill(*FILL_STRUCT.unpack_from(raw, off))

            latency_ns = egress_ns - rf.ingress_ns
            if latency_ns < 0:
                # egress before ingress means the clocks disagree: don't
                # let one bad sample pollute the statistics
                self.clock_errors += 1
                latency_ns = None
            else:
                self.latencies.append(latency_ns)
                self.sla_checked += 1
                if latency_ns > SLA_NS:
                    self.sla_breaches += 1
                    self.last_breach_t = time.perf_counter()

            self.fills.append(Fill(*rf, egress_ns, latency_ns))
            self.fill_total += 1
            if rf.kind == KIND_CANCEL:
                self.cancel_total += 1
            elif rf.kind == KIND_WHALE:
                self.whale_total += 1
                self.last_whale = (rf.side, rf.levels, rf.qty, rf.price)
                self.last_whale_t = time.perf_counter()
        return n_orders


class ThroughputMeter:
    """Orders per second over a rolling time window."""

    def __init__(self, window_s=1.0):
        self.window_s = window_s
        self.samples = deque()  # (timestamp, running total)
        self.total = 0
        self.peak = 0.0

    def record(self, count):
        now = time.perf_counter()
        self.total += count
        self.samples.append((now, self.total))
        while len(self.samples) > 1 and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()
        self.peak = max(self.peak, self.rate)

    @property
    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        t0, c0 = self.samples[0]
        t1, c1 = self.samples[-1]
        dt = t1 - t0
        return (c1 - c0) / dt if dt > 0 else 0.0


def min_samples(permille):
    """Fewest samples for a percentile to mean something (p99.9 -> 1000)."""
    return -(-1000 // (1000 - permille))  # ceil(1000 / (1000 - permille))


def percentile(sorted_vals, permille):
    """Nearest-rank percentile of an already sorted list, or None if the
    list is too short for that percentile to be meaningful."""
    n = len(sorted_vals)
    if n < min_samples(permille):
        return None
    k = (permille * n + 999) // 1000  # ceil(permille * n / 1000), whole numbers
    return sorted_vals[k - 1]


def latency_stats(samples_ns):
    """Latency samples in nanoseconds -> LatencyStats in microseconds."""
    if not samples_ns:
        return None
    us = [s / 1000 for s in samples_ns]
    ordered = sorted(us)
    pct = {name: percentile(ordered, pm) for name, pm in PCT_PERMILLE}
    over = len(ordered) - bisect_right(ordered, SLA_US)  # strictly above
    return LatencyStats(us[-1], sum(us) / len(us), ordered[0], ordered[-1],
                        len(us), pct, ordered, over)


def histogram(ordered, bins):
    """Log-spaced histogram of a sorted list.

    Returns (edges, counts): bin i covers edges[i] up to edges[i + 1].
    Each bin is wider than the one before it, so a long latency tail still
    fits on screen. The counts always add up to len(ordered).
    """
    n = len(ordered)
    if n == 0 or bins < 1:
        return [], []

    lo = max(ordered[0], HIST_LO_FLOOR_US)
    hi = ordered[-1]
    if bins == 1 or hi <= lo * 1.001:  # everything is (nearly) the same value
        return [lo, max(hi, lo)], [n]

    ratio = hi / lo
    edges = [lo * ratio ** (i / bins) for i in range(bins + 1)]
    edges[-1] = hi

    counts = []
    prev = 0
    for i in range(bins):
        idx = n if i == bins - 1 else bisect_left(ordered, edges[i + 1])
        counts.append(idx - prev)
        prev = idx
    return edges, counts


def bin_of(edges, value):
    """Index of the histogram bin that a value falls into."""
    i = bisect_right(edges, value) - 1
    return min(max(i, 0), len(edges) - 2)


def breach_state(snap):
    """(flashing, on): is the breach alert active, and is this frame the
    'lit' half of the flash? The flash toggles four times a second."""
    age = snap.breach_age
    if age is None or age > FLASH_S:
        return False, False
    return True, int(age * 4) % 2 == 0


def whale_state(snap):
    """While a whale alert is active: ((side, levels, qty, price), on, age),
    where 'on' is the lit half of the flash (five flashes a second).
    Otherwise None."""
    if snap.whale is None or snap.whale_age is None:
        return None
    if snap.whale_age > WHALE_FLASH_S:
        return None
    return snap.whale, int(snap.whale_age * 5) % 2 == 0, snap.whale_age


class SpreadTracker:
    """Notices when the best bid/ask spread changes between frames."""

    def __init__(self):
        self.last = None
        self.changed_t = None
        self.direction = 0  # +1 widened, -1 tightened

    def update(self, spread, now):
        if self.last is not None and abs(spread - self.last) > 1e-9:
            self.direction = 1 if spread > self.last else -1
            self.changed_t = now
        self.last = spread

    def active(self, now):
        """+1 / -1 while a recent shift is still highlighted, else 0."""
        if self.changed_t is None or now - self.changed_t > SHIFT_FLASH_S:
            return 0
        return self.direction


class EngineWorker(threading.Thread):
    """Background thread that consumes the engine at its own pace.

    The UI never touches the reader directly. It asks for a snapshot,
    so drawing can be slow without slowing the engine consumer down.
    """

    def __init__(self, reader):
        super().__init__(daemon=True)
        self.reader = reader
        self.meter = ThroughputMeter()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.polls = 0
        self.error = None

    def run(self):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    self.meter.record(self.reader.poll())
                    self.polls += 1
                time.sleep(WORKER_SLEEP)
        except Exception as exc:  # shown by the UI instead of freezing silently
            self.error = exc

    def snapshot(self):
        """Copy what the UI needs while holding the lock, then do the heavy
        work (sorting for percentiles) after the lock is released."""
        with self.lock:
            r = self.reader
            book = BookSnapshot(list(r.book.bids), list(r.book.asks))
            fills = list(r.fills)
            samples = list(r.latencies)
            rate = self.meter.rate
            peak = self.meter.peak
            total = self.meter.total
            fill_total = r.fill_total
            cancel_total = r.cancel_total
            whale_total = r.whale_total
            whale = r.last_whale
            last_whale_t = r.last_whale_t
            polls = self.polls
            clock_errors = r.clock_errors
            sla_checked = r.sla_checked
            sla_breaches = r.sla_breaches
            last_breach_t = r.last_breach_t
            ring_raw = None
            if hasattr(r.stream, "ring_stats"):
                cap, used, ring_peak = r.stream.ring_stats()
                ring_raw = (cap, used, ring_peak, r.ring_warns, r.ring_warn_t)

        now = time.perf_counter()
        breach_age = None if last_breach_t is None else now - last_breach_t
        whale_age = None if last_whale_t is None else now - last_whale_t
        ring = None
        if ring_raw is not None:
            warn_age = None if ring_raw[4] is None else now - ring_raw[4]
            ring = (*ring_raw[:4], warn_age)

        return Snapshot(
            book=book, rate=rate, peak=peak, total=total, fills=fills,
            fill_total=fill_total, cancel_total=cancel_total,
            whale_total=whale_total, whale=whale, whale_age=whale_age,
            latency=latency_stats(samples), polls=polls,
            clock_errors=clock_errors, sla_checked=sla_checked,
            sla_breaches=sla_breaches, breach_age=breach_age, ring=ring,
        )

    def stop(self):
        self.stop_event.set()
        self.join(timeout=1.0)


# ----------------------------------------------------- system telemetry

def read_rss_mb():
    """Current resident memory of this process in MB, or None.

    Uses `ps`, which exists on macOS and Linux, because the standard
    library can only report the *peak* (see peak_rss_mb).
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=1,
        )
        return int(out.stdout.strip()) / 1024  # ps reports kilobytes
    except Exception:
        return None


def peak_rss_mb():
    """Highest resident memory this process has used, in MB, or None."""
    try:
        import resource  # Unix only
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports kilobytes
        return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024
    except Exception:
        return None


class Telemetry(threading.Thread):
    """Samples this process's CPU and memory once a second.

    CPU is the share of one core used by the whole process, so 100% means
    one core fully busy and more than 100% means several threads at work.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.start_t = time.perf_counter()
        self.cpu = None                 # needs two samples, so empty at first
        self.rss_mb = read_rss_mb()

    def run(self):
        last_wall = time.perf_counter()
        last_cpu = time.process_time()
        while not self.stop_event.wait(TELEMETRY_S):
            try:
                wall = time.perf_counter()
                cpu_time = time.process_time()
                span = wall - last_wall
                cpu = (cpu_time - last_cpu) / span * 100 if span > 0 else None
                last_wall, last_cpu = wall, cpu_time
                rss = read_rss_mb()
                with self.lock:
                    self.cpu = cpu
                    self.rss_mb = rss
            except Exception:
                pass  # a failed sample must never kill the dashboard

    def snapshot(self):
        with self.lock:
            cpu, rss = self.cpu, self.rss_mb
        try:
            load = os.getloadavg()
        except (OSError, AttributeError):
            load = None
        return Health(
            cpu=cpu, load=load, cores=os.cpu_count() or 1, rss_mb=rss,
            peak_mb=peak_rss_mb(), threads=threading.active_count(),
            uptime=time.perf_counter() - self.start_t,
        )

    def stop(self):
        self.stop_event.set()
        self.join(timeout=1.5)


class LogView:
    """Scroll state of the trade log.

    offset = how many events back from the newest one the bottom row is.
    offset 0 means 'follow': the newest event is always visible.
    """

    def __init__(self):
        self.offset = 0
        self.follow = True
        self.last_total = 0

    def sync(self, snap):
        """Call once per frame. Keeps a scrolled-back view from drifting."""
        new = snap.fill_total - self.last_total
        self.last_total = snap.fill_total
        if not self.follow and new > 0:
            self.offset += new

    def scroll(self, delta):
        """delta > 0 goes back in history, delta < 0 goes toward newest."""
        self.offset = max(0, self.offset + delta)
        self.follow = self.offset == 0

    def resume(self):
        self.offset = 0
        self.follow = True


# ------------------------------------------------------------ drawing

def init_colors(enabled):
    """Set up the color pairs. Returns True when real colors are active.

    Green = fill, red = cancel, yellow = spread shift, magenta = whale.
    If the terminal has no color support (or colors are switched off) the
    styles fall back to plain attributes: cancel = underline, spread shift
    and whale = bold. The whale alert still flashes using reverse video.
    """
    COLORS["fill"] = 0
    COLORS["cancel"] = curses.A_UNDERLINE
    COLORS["spread"] = curses.A_BOLD
    COLORS["whale"] = curses.A_BOLD
    if not enabled or not curses.has_colors():
        return False
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1  # keep the terminal's own background
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, bg)
        curses.init_pair(2, curses.COLOR_RED, bg)
        curses.init_pair(3, curses.COLOR_YELLOW, bg)
        curses.init_pair(4, curses.COLOR_MAGENTA, bg)
    except curses.error:
        return False
    COLORS["fill"] = curses.color_pair(1)
    COLORS["cancel"] = curses.color_pair(2)
    COLORS["spread"] = curses.color_pair(3) | curses.A_BOLD
    COLORS["whale"] = curses.color_pair(4) | curses.A_BOLD
    return True


def put(win, y, x, text, attr=0):
    """addstr that never crashes or wraps if the window is too small."""
    try:
        _, w = win.getmaxyx()
        text = text[: max(0, w - x - 1)]
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def build_windows(stdscr):
    """Create the sub-windows for the current size, or None if too small.

    Wide terminals show everything at once: three panels on top (order
    book, metrics, system health) and the log next to the histogram below.
    Narrower ones show the health, log and histogram windows on the same
    spot, one at a time (key: h).
    """
    h, w = stdscr.getmaxyx()
    if h < MIN_H or w < MIN_W:
        return None

    header_h, footer_h = 1, 1
    body_h = h - header_h - footer_h
    top_h = body_h * 6 // 10
    log_h = body_h - top_h
    left_w = w // 2
    y = header_h + top_h
    wide = w >= WIDE_W

    try:
        wins = {
            "header": curses.newwin(header_h, w, 0, 0),
            "footer": curses.newwin(footer_h, w, h - 1, 0),
            "wide": wide,
        }
        if wide:
            health_x = 2 * TOP_COL_W
            wins["book"] = curses.newwin(top_h, TOP_COL_W, header_h, 0)
            wins["metrics"] = curses.newwin(top_h, TOP_COL_W, header_h, TOP_COL_W)
            wins["health"] = curses.newwin(top_h, w - health_x, header_h, health_x)
            wins["log"] = curses.newwin(log_h, LOG_W, y, 0)
            wins["hist"] = curses.newwin(log_h, w - LOG_W, y, LOG_W)
        else:
            wins["book"] = curses.newwin(top_h, left_w, header_h, 0)
            wins["metrics"] = curses.newwin(top_h, w - left_w, header_h, left_w)
            wins["health"] = curses.newwin(log_h, w, y, 0)
            wins["log"] = curses.newwin(log_h, w, y, 0)
            wins["hist"] = curses.newwin(log_h, w, y, 0)
        return wins
    except curses.error:
        return None


def draw_panel(win, title):
    win.erase()
    win.box()
    put(win, 0, 2, f" {title} ")


def draw_ladder_row(win, row, price, qty, bar_w, attr=0):
    bar = "#" * int(qty / MAX_QTY * bar_w)
    put(win, row, 2, f"{price:>10.2f}{qty:>8}  {bar}", attr)


def draw_header(win, snap, stats_text, ws):
    """Title and stats, or a full-width flashing banner during a whale sweep."""
    _, w = win.getmaxyx()
    win.erase()

    if ws:
        (side, levels, qty, price), on, _age = ws
        word = "BUY" if side == SIDE_BUY else "SELL"
        text = (f">>> WHALE SWEEP {word} {qty:,} | {levels} levels "
                f"| to {price:.2f} <<<")
        attr = COLORS["whale"] | curses.A_BOLD
        if on:
            attr |= curses.A_REVERSE
        put(win, 0, 0, text.center(w - 1), attr)
    else:
        put(win, 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
        stats_x = max(0, w - len(stats_text) - 3)
        put(win, 0, stats_x, stats_text)
        flashing, on = breach_state(snap)
        if flashing and stats_x - 34 >= 13:  # only when there is room for it
            put(win, 0, 34, " SLA BREACH ",
                curses.A_REVERSE if on else curses.A_BOLD)

    win.noutrefresh()


def draw_book(win, book, spread_dir, ws):
    draw_panel(win, "Order Book")
    h, w = win.getmaxyx()

    # rows needed: header + asks + spread + bids + 2 border rows
    levels = max(0, min(LEVELS, (h - 4) // 2))
    bar_w = max(0, w - 24)

    # a whale sweep flashes the price levels it just cleared
    sweep_side, sweep_n, sweep_attr = None, 0, 0
    if ws:
        (sweep_side, sweep_n, _qty, _price), on, _age = ws
        sweep_attr = COLORS["whale"] | curses.A_BOLD
        if on:
            sweep_attr |= curses.A_REVERSE

    put(win, 1, 2, f"{'PRICE':>10}{'QTY':>8}  DEPTH", curses.A_BOLD)

    # asks: furthest level on top, best ask just above the spread row.
    # A buy sweep clears the asks.
    for i in range(levels):
        row = 2 + (levels - 1 - i)
        price, qty = book.asks[i]
        if sweep_side == SIDE_BUY and i < sweep_n:
            put(win, row, 2, f"{price:>10.2f}{0:>8}  << SWEPT", sweep_attr)
        else:
            draw_ladder_row(win, row, price, qty, bar_w, curses.A_DIM)

    # spread row in the middle, yellow for a moment after the spread shifts
    spread_row = 2 + levels
    if spread_dir:
        word = "^ widened" if spread_dir > 0 else "v tightened"
        put(win, spread_row, 2, f"-- spread {book.spread:.2f}  {word} --",
            COLORS["spread"])
    else:
        put(win, spread_row, 2, f"-- spread {book.spread:.2f} --", curses.A_BOLD)

    # bids: best bid just below the spread row, then going down.
    # A sell sweep clears the bids.
    for i in range(levels):
        row = spread_row + 1 + i
        price, qty = book.bids[i]
        if sweep_side == SIDE_SELL and i < sweep_n:
            put(win, row, 2, f"{price:>10.2f}{0:>8}  << SWEPT", sweep_attr)
        else:
            draw_ladder_row(win, row, price, qty, bar_w)

    win.noutrefresh()


def fmt_pct(value):
    """A percentile value for the panel, '--' while there are too few samples."""
    return f"{value:>8.1f}" if value is not None else f"{'--':>8}"


def draw_metrics(win, snap):
    draw_panel(win, "Metrics")
    h, w = win.getmaxyx()

    put(win, 2, 2, f"{'Orders/sec':<12}{snap.rate:>14,.0f}", curses.A_BOLD)
    put(win, 3, 2, f"{'Peak':<12}{snap.peak:>14,.0f}")
    put(win, 4, 2, f"{'Total':<12}{snap.total:>14,}")

    # throughput bar relative to the target
    suffix = f" {TARGET_OPS:,}/s"
    bar_w = max(0, w - 6 - len(suffix))
    filled = min(bar_w, int(snap.rate / TARGET_OPS * bar_w))
    put(win, 5, 2, "[" + "#" * filled + "." * (bar_w - filled) + "]" + suffix)

    # latency = egress_ns - ingress_ns, shown in microseconds
    lat = snap.latency
    if lat is None:
        put(win, 6, 2, "Latency (us)", curses.A_BOLD)
        put(win, 7, 2, "waiting for samples...")
        win.noutrefresh()
        return

    put(win, 6, 2, f"Latency (us)  rolling n={lat.n:,}", curses.A_BOLD)
    put(win, 7, 2, f"{'last':<5}{lat.last:>8.1f}{'avg':>6}{lat.avg:>8.1f}")
    put(win, 8, 2, f"{'min':<5}{lat.min:>8.1f}{'max':>6}{lat.max:>8.1f}")
    put(win, 9, 2,
        f"{'p50':<5}{fmt_pct(lat.pct['p50'])}{'p90':>6}{fmt_pct(lat.pct['p90'])}")
    put(win, 10, 2,
        f"{'p99':<5}{fmt_pct(lat.pct['p99'])}{'p99.9':>6}{fmt_pct(lat.pct['p99.9'])}")

    # SLA counter and flashing alert
    rate = 100 * snap.sla_breaches / snap.sla_checked if snap.sla_checked else 0.0
    put(win, 11, 2,
        f"SLA >{SLA_US:g}us: {snap.sla_breaches:,} ({rate:.2f}%)", curses.A_BOLD)

    flashing, on = breach_state(snap)
    if flashing:
        status = "!! SLA BREACH !!"
        attr = (curses.A_REVERSE | curses.A_BOLD) if on else curses.A_BOLD
    elif snap.breach_age is None:
        status, attr = "SLA ok", curses.A_BOLD
    else:
        status, attr = f"last breach {snap.breach_age:.0f}s ago", 0
    put(win, 12, 2, status, attr)
    put(win, 12, 2 + len(status) + 2, f"win:{lat.over:,}")

    if snap.clock_errors:
        put(win, 13, 2, f"bad timestamps: {snap.clock_errors}", curses.A_REVERSE)
    elif lat.pct["p99.9"] is None:
        need = min_samples(999)
        put(win, 13, 2, f"p99.9 needs {need:,} samples", curses.A_DIM)

    win.noutrefresh()


def bar_text(frac, width):
    """A [####....] bar for a fraction between 0 and 1."""
    frac = min(max(frac, 0.0), 1.0)
    filled = int(round(frac * width))
    return "[" + "#" * filled + "." * max(0, width - filled) + "]"


def fmt_uptime(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def draw_health(win, snap, health):
    """System health: process CPU and memory, ring buffer fill, counters."""
    draw_panel(win, "System Health")
    h, w = win.getmaxyx()

    lines = []  # (text, attr); (None, 0) is a blank separator line

    # --- process CPU and memory
    cpu = health.cpu if health else None
    cpu_txt = f"{cpu:.1f}%" if cpu is not None else "--"
    bw = max(0, w - 4 - 16 - 2)
    lines.append((f"{'CPU':<8}{cpu_txt:>7} {bar_text((cpu or 0) / 100, bw)}",
                  curses.A_BOLD))

    rss = health.rss_mb if health else None
    peak = health.peak_mb if health else None
    lines.append((
        f"{'Memory':<8}"
        f"{f'{rss:.1f} MB' if rss is not None else '--'}  "
        f"(peak {f'{peak:.1f}' if peak is not None else '--'})", 0))

    if health and health.load:
        l1, l5, l15 = health.load
        lines.append((f"{'Load':<8}{l1:.2f} {l5:.2f} {l15:.2f}  "
                      f"({health.cores} cores)", 0))
    else:
        lines.append((f"{'Load':<8}n/a", curses.A_DIM))

    if health:
        lines.append((f"{'Threads':<8}{health.threads}   "
                      f"up {fmt_uptime(health.uptime)}", 0))
    lines.append((None, 0))

    # --- shared-memory ring buffer
    lines.append(("Shared memory ring (mock)", curses.A_BOLD))
    if snap.ring is None:
        lines.append(("no ring buffer data", curses.A_DIM))
    else:
        cap, used, ring_peak, warns, warn_age = snap.ring
        frac = used / cap if cap else 0.0
        hot = warn_age is not None and warn_age <= RING_FLASH_S
        blink = int(time.perf_counter() * 4) % 2 == 0

        bar_attr = 0
        if hot:
            bar_attr = curses.A_BOLD | (curses.A_REVERSE if blink else 0)
        lines.append((f"{frac * 100:5.1f}% "
                      f"{bar_text(frac, max(0, w - 4 - 7 - 2))}", bar_attr))
        lines.append((f"backlog {used:,}/{cap:,}  "
                      f"peak {ring_peak / cap * 100:.0f}%", 0))
        if hot:
            attr = COLORS["cancel"] | curses.A_BOLD
            if blink:
                attr |= curses.A_REVERSE
            lines.append((f"!! HIGH WATERMARK >{RING_WARN * 100:.0f}% !!", attr))
        else:
            alerts = f"  ({warns} alerts)" if warns else ""
            lines.append((f"Watermark OK{alerts}", curses.A_BOLD))
    lines.append((None, 0))

    # --- trade counters
    n_fills = snap.fill_total - snap.cancel_total
    lines.append((f"{'Orders':<8}{snap.total:,}", 0))
    lines.append((f"{'Fills':<8}{n_fills:,}   cancels {snap.cancel_total:,}", 0))
    lines.append((f"{'Whales':<8}{snap.whale_total:,}   "
                  f"SLA breaches {snap.sla_breaches:,}", 0))

    # on a short panel: drop the blank lines first, then the last rows
    avail = h - 2
    if len(lines) > avail:
        lines = [ln for ln in lines if ln[0] is not None]
    for i, (text, attr) in enumerate(lines[:avail]):
        if text is not None:
            put(win, 1 + i, 2, text, attr)

    win.noutrefresh()


def draw_hist(win, snap):
    """Live histogram of the rolling latency window, with percentile tags."""
    draw_panel(win, "Latency Histogram (us)")
    h, w = win.getmaxyx()

    lat = snap.latency
    if lat is None:
        put(win, 2, 2, "waiting for samples...")
        win.noutrefresh()
        return

    rows = h - 3  # row 1 = subtitle, rows 2.. = bins, last row = border
    if rows < 1:
        win.noutrefresh()
        return

    edges, counts = histogram(lat.ordered, min(rows, HIST_MAX_BINS))

    # which percentiles (and the SLA limit) fall into which bin
    tags = [[] for _ in counts]
    for name, value in lat.pct.items():
        if value is not None:
            tags[bin_of(edges, value)].append(name)
    if edges and edges[0] <= SLA_US <= edges[-1]:
        tags[bin_of(edges, SLA_US)].append("SLA")

    put(win, 1, 2, f"n={lat.n:,}  log scale  # ok  ! over {SLA_US:g}us",
        curses.A_BOLD)

    prefix_w = 2 + 13 + 7   # x offset + label + count columns
    tag_w = 13
    bar_w = max(0, w - prefix_w - tag_w - 1)
    biggest = max(counts) or 1

    for i, c in enumerate(counts):
        label = f"{edges[i]:>6.1f}-{edges[i + 1]:<6.1f}"
        mark = "!" if edges[i] >= SLA_US else "#"  # whole bin is over the SLA
        bar = ""
        if c > 0:
            bar = mark * max(1, round(c / biggest * bar_w))
        tag_text = "/".join(tags[i])
        line = f"{label} {c:>5} {bar:<{bar_w}} {tag_text}"
        if tags[i]:
            attr = curses.A_BOLD
        elif c == 0:
            attr = curses.A_DIM
        else:
            attr = 0
        put(win, 2 + i, 2, line, attr)

    win.noutrefresh()


def print_histogram(lat, bins=10, width=40):
    """Plain-text histogram for the end-of-run summary."""
    edges, counts = histogram(lat.ordered, bins)
    if not counts:
        return
    biggest = max(counts) or 1
    for i, c in enumerate(counts):
        mark = "!" if edges[i] >= SLA_US else "#"
        bar = mark * (max(1, round(c / biggest * width)) if c else 0)
        print(f"{edges[i]:>8.1f}-{edges[i + 1]:<8.1f} {c:>6}  {bar}")


def fmt_time(ts_ns):
    """Nanosecond timestamp -> HH:MM:SS.mmm in local time."""
    secs = ts_ns // 1_000_000_000
    ms = (ts_ns // 1_000_000) % 1000
    return time.strftime("%H:%M:%S", time.localtime(secs)) + f".{ms:03d}"


def fmt_latency(latency_ns):
    """Nanoseconds -> microseconds text, 'n/a' if the sample was invalid."""
    if latency_ns is None:
        return "n/a"
    return f"{latency_ns / 1000:.1f}"


def draw_log(win, snap, view):
    draw_panel(win, "Trade Log")
    h, _ = win.getmaxyx()

    # row 1 = status line, row 2 = column headers, rows 3.. = events
    rows = max(0, h - 4)
    total = len(snap.fills)
    view.offset = min(view.offset, max(0, total - rows))
    if view.offset == 0:
        view.follow = True

    if view.follow:
        status = "LIVE"
        status_attr = curses.A_BOLD
    else:
        status = f"PAUSED +{view.offset} (f: resume)"
        status_attr = curses.A_REVERSE
    n_fills = snap.fill_total - snap.cancel_total
    put(win, 1, 2,
        f"{status}  fills: {n_fills:,}  cancels: {snap.cancel_total:,}  "
        f"whales: {snap.whale_total:,}  over SLA: {snap.sla_breaches:,}",
        status_attr)
    put(win, 2, 2,
        f"{'TIME':<13}{'ORDER':<10} {'EVENT':<6}{'SIDE':<5}{'QTY':>5}   "
        f"{'PRICE':>8} {'LAT(us)':>8} {'SLA':<3}",
        curses.A_BOLD)

    now_ns = time.perf_counter_ns()
    end = total - view.offset
    start = max(0, end - rows)
    visible = snap.fills[start:end]
    for i, f in enumerate(visible):
        cancel = f.kind == KIND_CANCEL
        whale = f.kind == KIND_WHALE
        kind = "WHALE" if whale else ("CANCEL" if cancel else "FILL")
        side = "BUY" if f.side == SIDE_BUY else "SELL"
        breach = f.latency_ns is not None and f.latency_ns > SLA_NS
        line = (f"{fmt_time(f.ts_ns):<13}#{f.order_id:<9} {kind:<6}{side:<5}"
                f"{f.qty:>5} @ {f.price:>8.2f} {fmt_latency(f.latency_ns):>8} "
                f"{'!!' if breach else '':<3}")
        attr = COLORS["cancel"] if cancel else COLORS["fill"]
        if whale:
            attr = COLORS["whale"] | curses.A_BOLD
            age = (now_ns - f.egress_ns) / 1e9
            if age <= WHALE_FLASH_S and int(age * 5) % 2 == 0:
                attr |= curses.A_REVERSE  # flashing while the whale is recent
        if breach:
            attr |= curses.A_REVERSE
        if view.follow and i == len(visible) - 1:
            attr |= curses.A_BOLD
        put(win, 3 + i, 2, line, attr)

    win.noutrefresh()


def draw_footer(win, text):
    """Key hints on the left, a colored legend on the right if it fits."""
    _, w = win.getmaxyx()
    win.erase()
    put(win, 0, 1, text)

    legend = (("FILL", "fill"), ("CANCEL", "cancel"),
              ("SPREAD", "spread"), ("WHALE", "whale"))
    total = sum(len(name) for name, _ in legend) + 2 * (len(legend) - 1)
    x = w - total - 3
    if x > len(text) + 3:
        for name, key in legend:
            put(win, 0, x, name, COLORS[key] | curses.A_BOLD)
            x += len(name) + 2
    win.noutrefresh()


def draw(wins, snap, view, stats_text, bottom, spread_dir, health):
    ws = whale_state(snap)

    draw_header(wins["header"], snap, stats_text, ws)
    draw_book(wins["book"], snap.book, spread_dir, ws)
    draw_metrics(wins["metrics"], snap)

    if wins["wide"]:
        draw_health(wins["health"], snap, health)
        draw_log(wins["log"], snap, view)
        draw_hist(wins["hist"], snap)
        footer = "q quit  Up/Dn PgUp/PgDn scroll log  f/End follow  w test whale"
    else:
        if bottom == "hist":
            draw_hist(wins["hist"], snap)
        elif bottom == "health":
            draw_health(wins["health"], snap, health)
        else:
            draw_log(wins["log"], snap, view)
        footer = "q quit  Up/Dn scroll  f follow  h log/hist/health  w whale"

    draw_footer(wins["footer"], footer)
    curses.doupdate()


def draw_too_small(stdscr):
    """Shown instead of the dashboard when the terminal is below MIN size."""
    h, w = stdscr.getmaxyx()
    lines = [f"Terminal too small: {w}x{h}", f"Need at least {MIN_W}x{MIN_H}"]
    stdscr.erase()
    for i, text in enumerate(lines):
        put(stdscr, max(0, h // 2 - 1 + i), max(0, (w - len(text)) // 2), text)
    stdscr.noutrefresh()
    curses.doupdate()


def handle_resize(stdscr):
    """Re-read the terminal size, wipe stale content and rebuild the layout."""
    curses.update_lines_cols()
    stdscr.clear()
    stdscr.refresh()  # apply the clear now so getch() doesn't blank the screen later
    return build_windows(stdscr), stdscr.getmaxyx()


# --------------------------------------------------------------- main

BOTTOM_VIEWS = ("log", "hist", "health")


def main(stdscr, args):
    curses.curs_set(0)
    init_colors(USE_COLOR)
    stdscr.timeout(REFRESH_MS)

    size = stdscr.getmaxyx()
    wins = build_windows(stdscr)
    view = LogView()
    spread = SpreadTracker()
    bottom = "log"  # bottom panel on a narrow terminal: log, hist or health

    worker = EngineWorker(EngineReader(make_stream(args)))
    worker.start()
    telemetry = Telemetry()
    telemetry.start()

    fps = 0.0
    poll_rate = 0.0
    snap = None
    health = None
    start_t = time.perf_counter()
    last_t = start_t
    last_polls = 0

    try:
        while worker.error is None:
            snap = worker.snapshot()
            health = telemetry.snapshot()

            now = time.perf_counter()
            dt = now - last_t
            last_t = now
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1 / dt)
                poll_rate = 0.9 * poll_rate + 0.1 * ((snap.polls - last_polls) / dt)
            last_polls = snap.polls

            view.sync(snap)
            spread.update(snap.book.spread, now)

            if wins is None:
                draw_too_small(stdscr)
            else:
                stats_text = f"UI {fps:4.1f} fps | engine {poll_rate:,.0f} polls/s"
                draw(wins, snap, view, stats_text, bottom,
                     spread.active(now), health)

            if args.duration is not None and now - start_t >= args.duration:
                break

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            elif key == curses.KEY_UP:
                view.scroll(1)
            elif key == curses.KEY_DOWN:
                view.scroll(-1)
            elif key == curses.KEY_PPAGE:
                view.scroll(PAGE)
            elif key == curses.KEY_NPAGE:
                view.scroll(-PAGE)
            elif key in (ord("f"), ord("F"), curses.KEY_END):
                view.resume()
            elif key in (ord("h"), ord("H")):
                i = BOTTOM_VIEWS.index(bottom)
                bottom = BOTTOM_VIEWS[(i + 1) % len(BOTTOM_VIEWS)]
            elif key in (ord("w"), ord("W")):
                stream = worker.reader.stream
                if hasattr(stream, "request_whale"):  # mock source only
                    stream.request_whale()
            # KEY_RESIZE is the normal signal, the size check is a safety net
            if key == curses.KEY_RESIZE or stdscr.getmaxyx() != size:
                wins, size = handle_resize(stdscr)
    finally:
        worker.stop()
        telemetry.stop()

    return worker.error, snap, health


def pct_text(value):
    return f"{value:.1f}" if value is not None else "--"


if __name__ == "__main__":
    args = build_parser().parse_args()
    apply_args(args)
    error, last, health = curses.wrapper(main, args)
    if error:
        raise error
    if last:
        res_ns = time.get_clock_info("perf_counter").resolution * 1e9
        print(
            f"Session ended: {last.total:,} orders processed, "
            f"peak {last.peak:,.0f} orders/sec"
        )
        print(
            f"Events: {last.fill_total - last.cancel_total:,} fills "
            f"(including {last.whale_total:,} whale sweeps), "
            f"{last.cancel_total:,} cancels"
        )
        if health:
            cpu = f"{health.cpu:.0f}%" if health.cpu is not None else "--"
            peak = f"{health.peak_mb:.1f} MB" if health.peak_mb is not None else "--"
            print(
                f"Process: peak memory {peak}, CPU {cpu} of one core "
                f"(last sample), {health.threads} threads, {health.cores} cores"
            )
        if last.ring:
            cap, _used, ring_peak, warns, _age = last.ring
            print(
                f"Ring buffer (mock): peak {ring_peak / cap * 100:.0f}% full, "
                f"{warns} high-watermark alerts"
            )
        if last.sla_checked:
            print(
                f"SLA (> {SLA_US:g} us): {last.sla_breaches:,} breaches out of "
                f"{last.sla_checked:,} events "
                f"({100 * last.sla_breaches / last.sla_checked:.2f}%)"
            )
        if last.latency:
            lat = last.latency
            print(
                f"Latency (us) over last {lat.n:,} samples: "
                f"avg {lat.avg:.1f}, min {lat.min:.1f}, max {lat.max:.1f}"
            )
            print(
                "Percentiles (us): "
                + ", ".join(f"{name} {pct_text(v)}" for name, v in lat.pct.items())
                + f"  | clock resolution {res_ns:.0f} ns, "
                f"bad timestamps {last.clock_errors}"
            )
            print(f"Latency histogram (us, log scale, ! = over the "
                  f"{SLA_US:g} us SLA):")
            print_histogram(lat)
