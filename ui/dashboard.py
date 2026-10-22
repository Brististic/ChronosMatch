import argparse
import curses
import random
import struct
import threading
import time
from bisect import bisect_left, bisect_right
from collections import deque, namedtuple

VERSION = "0.4.0"

# Defaults. The launch flags below can override the first four at startup.
REFRESH_MS = 100       # UI refresh interval (about 10 frames/s)
LEVELS = 5             # price levels shown per side
TARGET_OPS = 100_000   # target throughput from the project plan
LATENCY_BUFFER = 5000  # rolling window: how many latency samples are kept

WORKER_SLEEP = 0.001   # engine consumer loop pause
TICK = 0.01            # price step between levels
MAX_QTY = 2000
FILL_BUFFER = 500      # how many recent fills the UI keeps

MIN_H, MIN_W = 28, 70  # smallest terminal the layout fits in
WIDE_W = 110           # at this width the histogram sits next to the log
LOG_W = 66             # width of the trade log in the wide layout
PAGE = 10              # lines moved by PgUp / PgDn

HIST_MAX_BINS = 20     # most rows the histogram will use
HIST_LO_FLOOR_US = 0.1 # lowest edge of the log-scale histogram

# Percentiles shown in the Metrics panel, in permille (per 1000) so the
# maths stays in whole numbers: 999 = 99.9th percentile.
PCT_PERMILLE = (("p50", 500), ("p90", 900), ("p99", 990), ("p99.9", 999))

# Binary layout of one matched fill coming from the engine.
# PLACEHOLDER: confirm the real layout with the IPC/engine members.
#   ts_ns      wall-clock time of the trade (for display)
#   ingress_ns time.perf_counter_ns() stamped when the order entered the system
FILL_STRUCT = struct.Struct("<QBIdQQ")
RawFill = namedtuple("RawFill", "order_id side qty price ts_ns ingress_ns")
# egress_ns is stamped by the UI reader when it decodes the fill.
# latency_ns = egress_ns - ingress_ns (None if the clocks disagree).
Fill = namedtuple(
    "Fill", "order_id side qty price ts_ns ingress_ns egress_ns latency_ns"
)
SIDE_BUY, SIDE_SELL = 0, 1

# n = samples in the window, pct = {"p50": value or None, ...},
# ordered = all window samples in microseconds, sorted (feeds the histogram)
LatencyStats = namedtuple("LatencyStats", "last avg min max n pct ordered")
Snapshot = namedtuple(
    "Snapshot",
    "book rate peak total fills fill_total latency polls clock_errors",
)


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
            "  python3 ui/dashboard.py --window 1000 --seed 42 --duration 30\n"
            "\n"
            "keys while running:\n"
            "  q quit | Up/Down scroll log | PgUp/PgDn page | f or End follow live\n"
            "  h switch the bottom panel between log and histogram (narrow "
            "terminals)"
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
    global REFRESH_MS, LEVELS, TARGET_OPS, LATENCY_BUFFER
    REFRESH_MS = args.refresh_ms
    LEVELS = args.levels
    TARGET_OPS = args.target_ops
    LATENCY_BUFFER = args.window
    if args.seed is not None:
        random.seed(args.seed)


# --------------------------------------------------------- mock engine

class MockOrderBook:
    """Fake multi-level book. Lives inside the mock stream below."""

    def __init__(self, mid=150.00):
        self.mid = mid
        self.bids = []  # list of (price, qty), best first
        self.asks = []  # list of (price, qty), best first
        self.update()

    def update(self):
        self.mid += random.uniform(-0.03, 0.03)
        best_bid = round(self.mid, 2)
        best_ask = round(best_bid + random.choice([1, 1, 2, 3]) * TICK, 2)
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

    Replace this class with the real stream. It only needs:
      - read() -> (orders_processed: int, fill_bytes: bytes)
        where every fill carries the ingress perf_counter_ns() stamp
      - book   -> object with .bids and .asks (lists of (price, qty))
    """

    def __init__(self, mid=150.00):
        self.book = MockOrderBook(mid)
        self.last = time.perf_counter()
        self.next_id = 1

    def read(self):
        now = time.perf_counter()
        dt = now - self.last
        self.last = now

        self.book.update()
        n_orders = int(TARGET_OPS * random.uniform(0.85, 1.10) * dt)

        raw = b""
        if random.random() < dt * 25:  # about 25 fills per second
            side = random.choice((SIDE_BUY, SIDE_SELL))
            price = self.book.bids[0][0] if side == SIDE_SELL else self.book.asks[0][0]
            qty = random.randint(10, 500)

            # Mock engine: pretend the order entered ~25 us ago by
            # backdating its ingress timestamp. Spikes: 5% are 3x slower,
            # about 0.2% are 10x slower (this is what P99.9 picks up).
            latency_us = max(1.0, random.gauss(25, 4))
            if random.random() < 0.05:
                latency_us *= 3
            if random.random() < 0.002:
                latency_us *= 10
            ingress_ns = time.perf_counter_ns() - int(latency_us * 1000)

            raw = FILL_STRUCT.pack(
                self.next_id, side, qty, price, time.time_ns(), ingress_ns
            )
            self.next_id += 1

        return n_orders, raw


def make_stream(args):
    """Create the data source chosen with --source."""
    if args.source == "mock":
        return MockEngineStream(args.mid_price)
    raise ValueError(f"unknown source: {args.source}")


class EngineReader:
    """Reads the engine stream, decodes fills and stamps their egress time."""

    def __init__(self, stream):
        self.stream = stream
        self.fills = deque(maxlen=FILL_BUFFER)
        self.latencies = deque(maxlen=LATENCY_BUFFER)  # nanoseconds, rolling
        self.fill_total = 0
        self.clock_errors = 0

    @property
    def book(self):
        return self.stream.book

    def poll(self):
        """Pull new data. Returns how many orders the engine processed."""
        n_orders, raw = self.stream.read()
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

            self.fills.append(Fill(*rf, egress_ns, latency_ns))
            self.fill_total += 1
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
    return LatencyStats(us[-1], sum(us) / len(us), ordered[0], ordered[-1],
                        len(us), pct, ordered)


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
            polls = self.polls
            clock_errors = r.clock_errors

        return Snapshot(
            book=book, rate=rate, peak=peak, total=total, fills=fills,
            fill_total=fill_total, latency=latency_stats(samples),
            polls=polls, clock_errors=clock_errors,
        )

    def stop(self):
        self.stop_event.set()
        self.join(timeout=1.0)


class LogView:
    """Scroll state of the trade log.

    offset = how many fills back from the newest one the bottom row is.
    offset 0 means 'follow': the newest trade is always visible.
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

    Wide terminals get the log and the histogram side by side. Narrow ones
    get both windows on the same spot and show one at a time (key: h).
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
            "book": curses.newwin(top_h, left_w, header_h, 0),
            "metrics": curses.newwin(top_h, w - left_w, header_h, left_w),
            "footer": curses.newwin(footer_h, w, h - 1, 0),
            "wide": wide,
        }
        if wide:
            wins["log"] = curses.newwin(log_h, LOG_W, y, 0)
            wins["hist"] = curses.newwin(log_h, w - LOG_W, y, LOG_W)
        else:
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


def draw_book(win, book):
    draw_panel(win, "Order Book")
    h, w = win.getmaxyx()

    # rows needed: header + asks + spread + bids + 2 border rows
    levels = max(0, min(LEVELS, (h - 4) // 2))
    bar_w = max(0, w - 24)

    put(win, 1, 2, f"{'PRICE':>10}{'QTY':>8}  DEPTH", curses.A_BOLD)

    # asks: furthest level on top, best ask just above the spread row
    for i in range(levels):
        row = 2 + (levels - 1 - i)
        price, qty = book.asks[i]
        draw_ladder_row(win, row, price, qty, bar_w, curses.A_DIM)

    # spread row in the middle
    spread_row = 2 + levels
    put(win, spread_row, 2, f"-- spread {book.spread:.2f} --", curses.A_BOLD)

    # bids: best bid just below the spread row, then going down
    for i in range(levels):
        row = spread_row + 1 + i
        price, qty = book.bids[i]
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
    bar_w = max(0, w - 6)
    filled = min(bar_w, int(snap.rate / TARGET_OPS * bar_w))
    put(win, 5, 2, "[" + "#" * filled + "." * (bar_w - filled) + "]")
    put(win, 6, 2, f"target {TARGET_OPS:,}/s")

    # latency = egress_ns - ingress_ns, shown in microseconds
    lat = snap.latency
    if lat is None:
        put(win, 8, 2, "Latency (us)", curses.A_BOLD)
        put(win, 9, 2, "waiting for samples...")
        win.noutrefresh()
        return

    put(win, 8, 2, f"Latency (us)  rolling n={lat.n:,}", curses.A_BOLD)
    put(win, 9, 2, f"{'last':<5}{lat.last:>8.1f}{'avg':>6}{lat.avg:>8.1f}")
    put(win, 10, 2, f"{'min':<5}{lat.min:>8.1f}{'max':>6}{lat.max:>8.1f}")
    put(win, 11, 2,
        f"{'p50':<5}{fmt_pct(lat.pct['p50'])}{'p90':>6}{fmt_pct(lat.pct['p90'])}")
    put(win, 12, 2,
        f"{'p99':<5}{fmt_pct(lat.pct['p99'])}{'p99.9':>6}{fmt_pct(lat.pct['p99.9'])}")

    if snap.clock_errors:
        put(win, 13, 2, f"bad timestamps: {snap.clock_errors}", curses.A_REVERSE)
    elif lat.pct["p99.9"] is None:
        need = min_samples(999)
        put(win, 13, 2, f"p99.9 needs {need:,} samples", curses.A_DIM)

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

    # which percentiles fall into which bin
    tags = [[] for _ in counts]
    for name, value in lat.pct.items():
        if value is not None:
            tags[bin_of(edges, value)].append(name)

    put(win, 1, 2, f"n={lat.n:,}  log scale  # = count", curses.A_BOLD)

    prefix_w = 2 + 13 + 7   # x offset + label + count columns
    tag_w = 11
    bar_w = max(0, w - prefix_w - tag_w - 1)
    biggest = max(counts) or 1

    for i, c in enumerate(counts):
        label = f"{edges[i]:>6.1f}-{edges[i + 1]:<6.1f}"
        bar = ""
        if c > 0:
            bar = "#" * max(1, round(c / biggest * bar_w))
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
        bar = "#" * (max(1, round(c / biggest * width)) if c else 0)
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

    # row 1 = status line, row 2 = column headers, rows 3.. = trades
    rows = max(0, h - 4)
    total = len(snap.fills)
    view.offset = min(view.offset, max(0, total - rows))
    if view.offset == 0:
        view.follow = True

    if view.follow:
        status = "LIVE"
        status_attr = curses.A_BOLD
    else:
        status = f"PAUSED  +{view.offset} newer  (f: resume)"
        status_attr = curses.A_REVERSE
    put(win, 1, 2, f"{status}   fills: {snap.fill_total:,}", status_attr)
    put(win, 2, 2,
        f"{'TIME':<13}{'ORDER':<11}{'SIDE':<6}{'QTY':>6}   {'PRICE':>9} {'LAT(us)':>9}",
        curses.A_BOLD)

    end = total - view.offset
    start = max(0, end - rows)
    visible = snap.fills[start:end]
    for i, f in enumerate(visible):
        side = "BUY" if f.side == SIDE_BUY else "SELL"
        line = (f"{fmt_time(f.ts_ns):<13}#{f.order_id:<10}{side:<6}"
                f"{f.qty:>6} @ {f.price:>9.2f} {fmt_latency(f.latency_ns):>9}")
        newest = view.follow and i == len(visible) - 1
        put(win, 3 + i, 2, line, curses.A_BOLD if newest else 0)

    win.noutrefresh()


def draw(wins, snap, view, stats_text, bottom):
    h, w = wins["header"].getmaxyx()
    wins["header"].erase()
    put(wins["header"], 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    put(wins["header"], 0, max(0, w - len(stats_text) - 3), stats_text)
    wins["header"].noutrefresh()

    draw_book(wins["book"], snap.book)
    draw_metrics(wins["metrics"], snap)

    if wins["wide"]:
        draw_log(wins["log"], snap, view)
        draw_hist(wins["hist"], snap)
        footer = "q: quit   Up/Down PgUp/PgDn: scroll log   f/End: follow"
    else:
        if bottom == "hist":
            draw_hist(wins["hist"], snap)
        else:
            draw_log(wins["log"], snap, view)
        footer = "q: quit  Up/Down PgUp/PgDn: scroll log  f: follow  h: log/histogram"

    wins["footer"].erase()
    put(wins["footer"], 0, 1, footer)
    wins["footer"].noutrefresh()

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

def main(stdscr, args):
    curses.curs_set(0)
    stdscr.timeout(REFRESH_MS)

    size = stdscr.getmaxyx()
    wins = build_windows(stdscr)
    view = LogView()
    bottom = "log"  # which panel a narrow terminal shows: "log" or "hist"

    worker = EngineWorker(EngineReader(make_stream(args)))
    worker.start()

    fps = 0.0
    poll_rate = 0.0
    snap = None
    start_t = time.perf_counter()
    last_t = start_t
    last_polls = 0

    try:
        while worker.error is None:
            snap = worker.snapshot()

            now = time.perf_counter()
            dt = now - last_t
            last_t = now
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1 / dt)
                poll_rate = 0.9 * poll_rate + 0.1 * ((snap.polls - last_polls) / dt)
            last_polls = snap.polls

            view.sync(snap)

            if wins is None:
                draw_too_small(stdscr)
            else:
                stats_text = f"UI {fps:4.1f} fps | engine {poll_rate:,.0f} polls/s"
                draw(wins, snap, view, stats_text, bottom)

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
                bottom = "hist" if bottom == "log" else "log"
            # KEY_RESIZE is the normal signal, the size check is a safety net
            if key == curses.KEY_RESIZE or stdscr.getmaxyx() != size:
                wins, size = handle_resize(stdscr)
    finally:
        worker.stop()

    return worker.error, snap


def pct_text(value):
    return f"{value:.1f}" if value is not None else "--"


if __name__ == "__main__":
    args = build_parser().parse_args()
    apply_args(args)
    error, last = curses.wrapper(main, args)
    if error:
        raise error
    if last:
        res_ns = time.get_clock_info("perf_counter").resolution * 1e9
        print(
            f"Session ended: {last.total:,} orders processed, "
            f"peak {last.peak:,.0f} orders/sec, {last.fill_total:,} fills"
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
            print("Latency histogram (us, log scale):")
            print_histogram(lat)
