import curses

REFRESH_MS = 100  # UI refresh interval


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
    win.addstr(0, 2, f" {title} ")
    win.noutrefresh()


def draw(wins):
    wins["header"].erase()
    wins["header"].addstr(0, 1, "ChronosMatch :: Latency Monitor", curses.A_BOLD)
    wins["header"].noutrefresh()

    draw_panel(wins["book"], "Order Book")
    draw_panel(wins["metrics"], "Latency (us)")
    draw_panel(wins["log"], "Trade Log")

    wins["footer"].erase()
    wins["footer"].addstr(0, 1, "q: quit")
    wins["footer"].noutrefresh()

    curses.doupdate()


def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(REFRESH_MS)

    wins = build_windows(stdscr)
    while True:
        draw(wins)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key == curses.KEY_RESIZE:
            stdscr.clear()
            wins = build_windows(stdscr)


if __name__ == "__main__":
    curses.wrapper(main)
