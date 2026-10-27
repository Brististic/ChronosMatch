"""Fake engine process for testing the dashboard's live (shared memory) source.

It writes ChronosMatch events into a memory-mapped ring buffer file, using
the same layout the dashboard reads (see the RING_* constants in
dashboard.py). The events come from the dashboard's built-in simulator, so
they include fills, cancels, whale sweeps and book snapshots.

  Terminal 1:  python3 ui/fake_producer.py
  Terminal 2:  python3 ui/dashboard.py --source shm

Press Ctrl+C to stop it. After restarting the producer, restart the
dashboard too: the producer creates a brand new ring file.
"""
import argparse
import mmap
import os
import struct
import time

import dashboard as d


def create_ring(path, capacity, slot_size):
    """Create a fresh ring file and map it for writing."""
    size = d.RING_OFFSET + capacity * slot_size
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.truncate(size)
    os.replace(tmp, path)  # a new file: a dashboard on an old one is unharmed
    f = open(path, "r+b")
    mm = mmap.mmap(f.fileno(), size)
    d.HDR_STRUCT.pack_into(
        mm, 0, d.RING_MAGIC, d.RING_VERSION, slot_size, capacity, 0, 0
    )
    return f, mm


def write_book(mm, book, seq):
    """Write a book snapshot. seq is odd while writing, even when complete."""
    struct.pack_into("<Q", mm, d.BOOK_OFF, seq + 1)
    nb = min(len(book.bids), d.BOOK_LEVELS)
    na = min(len(book.asks), d.BOOK_LEVELS)
    struct.pack_into("<II", mm, d.BOOK_OFF + 8, nb, na)
    base = d.BOOK_OFF + 16
    for i, (price, qty) in enumerate(book.bids[:nb]):
        d.BOOK_ENTRY.pack_into(mm, base + i * 16, price, qty, 0)
    ask_base = base + d.BOOK_LEVELS * 16
    for i, (price, qty) in enumerate(book.asks[:na]):
        d.BOOK_ENTRY.pack_into(mm, ask_base + i * 16, price, qty, 0)
    struct.pack_into("<Q", mm, d.BOOK_OFF, seq + 2)
    return seq + 2


def main():
    ap = argparse.ArgumentParser(description="Fake ChronosMatch engine "
                                 "writing into a shared-memory ring buffer.")
    ap.add_argument("--path", default=d.RING_PATH,
                    help=f"ring file to create (default {d.RING_PATH})")
    ap.add_argument("--capacity", type=int, default=65_536,
                    help="ring capacity in event slots (default 65536)")
    ap.add_argument("--slot-size", type=int, default=64,
                    help="bytes per slot, at least "
                         f"{d.FILL_STRUCT.size} (default 64)")
    ap.add_argument("--orders-per-sec", type=int, default=100_000,
                    help="simulated order rate (default 100000)")
    ap.add_argument("--whale-every", type=float, default=12.0,
                    help="average seconds between whale sweeps (default 12)")
    ap.add_argument("--seconds", type=float, default=None,
                    help="stop after this many seconds (default: until Ctrl+C)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    size = d.FILL_STRUCT.size
    if args.slot_size < size:
        ap.error(f"--slot-size must be at least {size}")

    # the simulator reads these settings from the dashboard module
    d.TARGET_OPS = args.orders_per_sec
    d.WHALE_EVERY_S = args.whale_every
    if args.seed is not None:
        d.random.seed(args.seed)

    f, mm = create_ring(args.path, args.capacity, args.slot_size)
    stream = d.MockEngineStream()
    head = orders = book_seq = 0
    last_book = 0.0
    start = time.perf_counter()
    print(f"fake engine writing to {args.path}  (Ctrl+C to stop)")

    try:
        while args.seconds is None or time.perf_counter() - start < args.seconds:
            n_orders, raw = stream.read()

            for off in range(0, len(raw), size):
                slot = d.RING_OFFSET + (head % args.capacity) * args.slot_size
                mm[slot:slot + size] = raw[off:off + size]  # slot first ...
                head += 1
            orders += n_orders
            struct.pack_into("<QQ", mm, d.HEAD_OFF, head, orders)  # ... then head

            now = time.perf_counter()
            if now - last_book >= 0.02:  # a fresh book snapshot every 20 ms
                book_seq = write_book(mm, stream.book, book_seq)
                last_book = now
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"stopped: {head:,} events, {orders:,} orders written")
        mm.close()
        f.close()


if __name__ == "__main__":
    main()
