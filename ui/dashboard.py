import curses
import random

REFRESH_MS = 100  # UI refresh interval
LEVELS = 5        # price levels shown per side
TICK = 0.01       # price step between levels
MAX_QTY = 2000


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


def put(win, y, x, text, attr=0):
    """addstr that doesn't crash if the window is too small."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def build_windows(stdscr):
    h, w = stdscr.getmaxyx()
    header_h, footer_h = 1, 1
    body_h = h - header_h - footer_h
    top_h = body_h * 6 // 10
    log_h = body_h - top_h
    left_w = w // 2

    return {
        "header": curses.newwin(header_h, w, 0, 0),
        "book": curses.newwin(top_h, left_w, header_h, 0),
        "metrics": curses.newwin(top_h, w - left_w, header_h, left_w),
        "log": curses.newwin(log_h, w, header_h + top_h, 0),
        "footer": curses.newwin(footer_h, w, h - 1, 0),
    }


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


def draw(wins, book):
    wins["header"].erase()
    put(wins["header"], 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    wins["header"].noutrefresh()

    draw_book(wins["book"], book)

    draw_panel(wins["metrics"], "Latency (us)")
    wins["metrics"].noutrefresh()

    draw_panel(wins["log"], "Trade Log")
    wins["log"].noutrefresh()

    wins["footer"].erase()
    put(wins["footer"], 0, 1, "q: quit")
    wins["footer"].noutrefresh()

    curses.doupdate()


def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(REFRESH_MS)

    wins = build_windows(stdscr)
    book = MockOrderBook()
    while True:
        book.update()
        draw(wins, book)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key == curses.KEY_RESIZE:
            stdscr.clear()
            wins = build_windows(stdscr)


if __name__ == "__main__":
    curses.wrapper(main)
