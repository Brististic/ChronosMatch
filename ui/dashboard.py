import curses
import random
import time
from collections import deque

REFRESH_MS = 100       # UI refresh interval
LEVELS = 5             # price levels shown per side
TICK = 0.01            # price step between levels
MAX_QTY = 2000
TARGET_OPS = 100_000   # target throughput from the project plan

MIN_H, MIN_W = 20, 70  # smallest terminal the layout fits in


class MockOrderBook:
    """Fake multi-level book. Replaced by the real engine output on Day 6."""

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


class MockOrderFeed:
    """Pretends the engine processed orders. Replaced by the real feed on Day 6."""

    def __init__(self):
        self.last = time.perf_counter()

    def poll(self):
        """Return how many orders were 'processed' since the last call."""
        now = time.perf_counter()
        dt = now - self.last
        self.last = now
        rate = TARGET_OPS * random.uniform(0.85, 1.10)
        return int(rate * dt)


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


def draw_metrics(win, meter):
    draw_panel(win, "Metrics")
    h, w = win.getmaxyx()

    put(win, 2, 2, f"{'Orders/sec':<12}{meter.rate:>14,.0f}", curses.A_BOLD)
    put(win, 3, 2, f"{'Peak':<12}{meter.peak:>14,.0f}")
    put(win, 4, 2, f"{'Total':<12}{meter.total:>14,}")

    # throughput bar relative to the 100k target
    bar_w = max(0, w - 6)
    filled = min(bar_w, int(meter.rate / TARGET_OPS * bar_w))
    put(win, 6, 2, "[" + "#" * filled + "." * (bar_w - filled) + "]")
    put(win, 7, 2, f"target {TARGET_OPS:,}/s")

    win.noutrefresh()


def draw(wins, book, meter):
    wins["header"].erase()
    put(wins["header"], 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    wins["header"].noutrefresh()

    draw_book(wins["book"], book)
    draw_metrics(wins["metrics"], meter)

    draw_panel(wins["log"], "Trade Log")
    wins["log"].noutrefresh()

    wins["footer"].erase()
    put(wins["footer"], 0, 1, "q: quit")
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
    book = MockOrderBook()
    feed = MockOrderFeed()
    meter = ThroughputMeter()

    while True:
        book.update()
        meter.record(feed.poll())

        if wins is None:
            draw_too_small(stdscr)
        else:
            draw(wins, book, meter)

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        # KEY_RESIZE is the normal signal, the size check is a safety net
        if key == curses.KEY_RESIZE or stdscr.getmaxyx() != size:
            wins, size = handle_resize(stdscr)


if __name__ == "__main__":
    curses.wrapper(main)
