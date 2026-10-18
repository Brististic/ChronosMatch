import curses
import random
import struct
import threading
import time
from collections import deque, namedtuple

REFRESH_MS = 100       # UI refresh interval (about 10 frames/s)
WORKER_SLEEP = 0.001   # engine consumer loop pause
LEVELS = 5             # price levels shown per side
TICK = 0.01            # price step between levels
MAX_QTY = 2000
TARGET_OPS = 100_000   # target throughput from the project plan
FILL_BUFFER = 500      # how many recent fills the UI keeps
LATENCY_BUFFER = 5000  # how many latency samples are kept

MIN_H, MIN_W = 26, 70  # smallest terminal the layout fits in
PAGE = 10              # lines moved by PgUp / PgDn

# Binary layout of one matched fill coming from the engine.
# PLACEHOLDER: confirm the real layout with the IPC/engine members.
FILL_STRUCT = struct.Struct("<QBIdQ")  # order_id, side, qty, price, ts_ns
Fill = namedtuple("Fill", "order_id side qty price ts_ns")
SIDE_BUY, SIDE_SELL = 0, 1

LatencyStats = namedtuple("LatencyStats", "last avg min max")
Snapshot = namedtuple(
    "Snapshot", "book rate peak total fills fill_total latency polls"
)


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
      - read() -> (orders_processed: int, fill_bytes: bytes, latency_us: float)
      - book   -> object with .bids and .asks (lists of (price, qty))
    """

    def __init__(self):
        self.book = MockOrderBook()
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
            raw = FILL_STRUCT.pack(self.next_id, side, qty, price, time.time_ns())
            self.next_id += 1

        # mock engine latency: around 25 us with occasional spikes
        latency_us = max(1.0, random.gauss(25, 4))
        if random.random() < 0.01:
            latency_us *= 3
        return n_orders, raw, latency_us


class EngineReader:
    """Reads the engine stream, decodes fills and keeps them in buffers."""

    def __init__(self, stream):
        self.stream = stream
        self.fills = deque(maxlen=FILL_BUFFER)
        self.latencies = deque(maxlen=LATENCY_BUFFER)
        self.fill_total = 0

    @property
    def book(self):
        return self.stream.book

    def poll(self):
        """Pull new data. Returns how many orders the engine processed."""
        n_orders, raw, latency_us = self.stream.read()
        self.latencies.append(latency_us)
        size = FILL_STRUCT.size
        for off in range(0, len(raw) - size + 1, size):
            self.fills.append(Fill(*FILL_STRUCT.unpack_from(raw, off)))
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


def latency_stats(samples):
    if not samples:
        return None
    return LatencyStats(samples[-1], sum(samples) / len(samples),
                        min(samples), max(samples))


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
        """Copy everything the UI needs, holding the lock only briefly."""
        with self.lock:
            r = self.reader
            return Snapshot(
                book=BookSnapshot(list(r.book.bids), list(r.book.asks)),
                rate=self.meter.rate,
                peak=self.meter.peak,
                total=self.meter.total,
                fills=list(r.fills),
                fill_total=r.fill_total,
                latency=latency_stats(list(r.latencies)),
                polls=self.polls,
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


def put(win, y, x, text, attr=0):
    """addstr that never crashes or wraps if the window is too small."""
    try:
        _, w = win.getmaxyx()
        text = text[: max(0, w - x - 1)]
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def build_windows(stdscr):
    """Create the sub-windows for the current size, or None if too small."""
    h, w = stdscr.getmaxyx()
    if h < MIN_H or w < MIN_W:
        return None

    header_h, footer_h = 1, 1
    body_h = h - header_h - footer_h
    top_h = body_h * 6 // 10
    log_h = body_h - top_h
    left_w = w // 2

    try:
        return {
            "header": curses.newwin(header_h, w, 0, 0),
            "book": curses.newwin(top_h, left_w, header_h, 0),
            "metrics": curses.newwin(top_h, w - left_w, header_h, left_w),
            "log": curses.newwin(log_h, w, header_h + top_h, 0),
            "footer": curses.newwin(footer_h, w, h - 1, 0),
        }
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


def draw_metrics(win, snap):
    draw_panel(win, "Metrics")
    h, w = win.getmaxyx()

    put(win, 2, 2, f"{'Orders/sec':<12}{snap.rate:>14,.0f}", curses.A_BOLD)
    put(win, 3, 2, f"{'Peak':<12}{snap.peak:>14,.0f}")
    put(win, 4, 2, f"{'Total':<12}{snap.total:>14,}")

    # throughput bar relative to the 100k target
    bar_w = max(0, w - 6)
    filled = min(bar_w, int(snap.rate / TARGET_OPS * bar_w))
    put(win, 6, 2, "[" + "#" * filled + "." * (bar_w - filled) + "]")
    put(win, 7, 2, f"target {TARGET_OPS:,}/s")

    # latency in microseconds
    put(win, 9, 2, "Latency (us)", curses.A_BOLD)
    lat = snap.latency
    if lat is None:
        put(win, 10, 2, "waiting for samples...")
    else:
        put(win, 10, 2, f"{'last':<5}{lat.last:>8.1f}{'avg':>6}{lat.avg:>8.1f}")
        put(win, 11, 2, f"{'min':<5}{lat.min:>8.1f}{'max':>6}{lat.max:>8.1f}")

    win.noutrefresh()


def fmt_time(ts_ns):
    """Nanosecond timestamp -> HH:MM:SS.mmm in local time."""
    secs = ts_ns // 1_000_000_000
    ms = (ts_ns // 1_000_000) % 1000
    return time.strftime("%H:%M:%S", time.localtime(secs)) + f".{ms:03d}"


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
    put(win, 2, 2, f"{'TIME':<13}{'ORDER':<11}{'SIDE':<6}{'QTY':>6}   {'PRICE':>9}",
        curses.A_BOLD)

    end = total - view.offset
    start = max(0, end - rows)
    visible = snap.fills[start:end]
    for i, f in enumerate(visible):
        side = "BUY" if f.side == SIDE_BUY else "SELL"
        line = (f"{fmt_time(f.ts_ns):<13}#{f.order_id:<10}{side:<6}"
                f"{f.qty:>6} @ {f.price:>9.2f}")
        newest = view.follow and i == len(visible) - 1
        put(win, 3 + i, 2, line, curses.A_BOLD if newest else 0)

    win.noutrefresh()


def draw(wins, snap, view, stats_text):
    h, w = wins["header"].getmaxyx()
    wins["header"].erase()
    put(wins["header"], 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    put(wins["header"], 0, max(0, w - len(stats_text) - 3), stats_text)
    wins["header"].noutrefresh()

    draw_book(wins["book"], snap.book)
    draw_metrics(wins["metrics"], snap)
    draw_log(wins["log"], snap, view)

    wins["footer"].erase()
    put(wins["footer"], 0, 1,
        "q: quit   Up/Down: scroll log   PgUp/PgDn: page   f/End: follow")
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


def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(REFRESH_MS)

    size = stdscr.getmaxyx()
    wins = build_windows(stdscr)
    view = LogView()

    worker = EngineWorker(EngineReader(MockEngineStream()))  # swap in the real stream later
    worker.start()

    fps = 0.0
    poll_rate = 0.0
    last_t = time.perf_counter()
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
                draw(wins, snap, view, stats_text)

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
            # KEY_RESIZE is the normal signal, the size check is a safety net
            if key == curses.KEY_RESIZE or stdscr.getmaxyx() != size:
                wins, size = handle_resize(stdscr)
    finally:
        worker.stop()

    return worker.error


if __name__ == "__main__":
    error = curses.wrapper(main)
    if error:
        raise error
