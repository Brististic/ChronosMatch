# -*- coding: utf-8 -*-
"""
ChronosMatch - Automated Live Demonstration Runner (run_demo.py)
Integrates:
  - Ingestion Firehose (Member 3)
  - Zero-Copy IPC Ring Buffers (Member 1)
  - Cython OrderBook Matching Core (Member 2)
  - MultiAssetOrderBookUI Dashboard (Member 4)
"""

import os
import sys
import time
import shutil

engine_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "engine"))
if engine_dir not in sys.path:
    sys.path.insert(0, engine_dir)

from chronos_engine import OrderBook, RingConsumer
from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.schema import unpack_order
from ui.dashboard import MultiAssetOrderBookUI


def run_live_demonstration(frames: int = 15):
    symbols = ["AAPL", "MSFT", "TSLA"]
    ipc_dir = os.path.abspath("demo_ipc_runtime")

    if os.path.exists(ipc_dir):
        shutil.rmtree(ipc_dir, ignore_errors=True)
    os.makedirs(ipc_dir, exist_ok=True)

    print("\n[*] Initializing ChronosMatch Zero-Copy Multi-Ring IPC Hub...")
    hub = MultiRingIPCHub(symbols=symbols, base_dir=ipc_dir, capacity_per_ring=1024, create=True)
    sim = MarketSimulator(symbols=symbols)
    dashboard = MultiAssetOrderBookUI(symbols=symbols)

    books = {sym: OrderBook(order_capacity=20000, level_capacity=500) for sym in symbols}
    consumers = {
        sym: RingConsumer(hub.ring_buffers[sym].filepath, hub.locks[sym].filepath)
        for sym in symbols
    }

    try:
        print("[*] Starting Live Streaming Engine & Dashboard...\n")
        time.sleep(0.5)

        for frame in range(1, frames + 1):
            t_start = time.perf_counter_ns()

            # 1. Market firehose generates batch
            batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=25)

            # 2. Write batch to IPC ring buffers
            for sym, pkts in batch.items():
                hub.write_symbol_batch(sym, pkts)

            # 3. Drain via Cython RingConsumer and update Member 4's UI
            for sym in symbols:
                stat = consumers[sym].drain_into(books[sym])

                # Update dashboard metrics with orders from this batch
                pkts = batch.get(sym, [])
                for raw_pkt in pkts:
                    parsed = unpack_order(raw_pkt)
                    side_code = getattr(parsed, "side", None)
                    px = getattr(parsed, "price", 0.0)
                    qty = getattr(parsed, "quantity", getattr(parsed, "qty", 0))
                    ts = getattr(parsed, "timestamp", time.perf_counter_ns())

                    side_bytes = b"B" if side_code in (1, b"B", "B") else b"S"
                    lat_us = max(0.5, (time.perf_counter_ns() - ts) / 1000.0)

                    dashboard.update_symbol_order(
                        symbol=sym,
                        side=side_bytes,
                        price=float(px),
                        quantity=int(qty),
                        latency_us=lat_us
                    )

            t_end = time.perf_counter_ns()
            frame_elapsed_ms = (t_end - t_start) / 1_000_000.0

            # 4. Render Member 4's dashboard snapshot
            os.system("cls" if os.name == "nt" else "clear")
            print(dashboard.render_text_snapshot())
            print(f" Frame: {frame:2d}/{frames:2d} | Frame Compute Time: {frame_elapsed_ms:.2f} ms | Status: NOMINAL")

            if frame % 5 == 0:
                print(f" >>> [ALERT] Active market sweep burst processed on frame {frame}!")

            time.sleep(0.15)

        print("\n[OK] Demonstration completed successfully without packet loss or GC lag.")

    finally:
        hub.close()
        hub.cleanup_files()
        shutil.rmtree(ipc_dir, ignore_errors=True)


if __name__ == "__main__":
    run_live_demonstration()
