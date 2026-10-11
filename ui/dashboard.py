import curses
import random

REFRESH_MS = 100  # UI refresh interval


class MockTopOfBook:
    """Fake best bid/ask feed. Replaced by the real engine output on Day 6."""

    def __init__(self, mid=150.00):
        self.mid = mid
        self.bid = mid - 0.01
        self.ask = mid + 0.01
        self.bid_qty = 500
        self.ask_qty = 500

    def update(self):
        self.mid += random.uniform(-0.03, 0.03)
        half_spread = random.choice([0.01, 0.01, 0.02, 0.03]) / 2
        self.bid = round(self.mid - half_spread, 2)
        self.ask = round(self.mid + half_spread, 2)
        self.bid_qty = random.randint(100, 2000)
        self.ask_qty = random.randint(100, 2000)

    @property
    def spread(self):
        return round(self.ask - self.bid, 2)


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


def draw_book(win, tob):
    draw_panel(win, "Order Book")
    put(win, 2, 2, f"{'':<10}{'PRICE':>10}{'QTY':>10}", curses.A_BOLD)
    put(win, 3, 2, f"{'BEST ASK':<10}{tob.ask:>10.2f}{tob.ask_qty:>10}")
    put(win, 4, 2, f"{'BEST BID':<10}{tob.bid:>10.2f}{tob.bid_qty:>10}")
    put(win, 6, 2, f"{'SPREAD':<10}{tob.spread:>10.2f}", curses.A_BOLD)
    put(win, 7, 2, f"{'MID':<10}{(tob.bid + tob.ask) / 2:>10.2f}")
    win.noutrefresh()


def draw(wins, tob):
    wins["header"].erase()
    put(wins["header"], 0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    wins["header"].noutrefresh()

    draw_book(wins["book"], tob)

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
    tob = MockTopOfBook()
    while True:
        tob.update()
        draw(wins, tob)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key == curses.KEY_RESIZE:
            stdscr.clear()
            wins = build_windows(stdscr)


if __name__ == "__main__":
    curses.wrapper(main)
