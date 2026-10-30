# -*- coding: utf-8 -*-
"""
ChronosMatch - Day 21 Multi-Process IPC Architecture (Member 1 & Member 3)
Runs independent OS processes across shared-memory ring buffers:
  - Process A (Market Producer): Continuous batch streaming into MultiRingIPCHub
  - Process B (Matching Engine Consumer): Cython order book ingestion & drain loop
"""

import os
import sys
import time
import multiprocessing as mp
from typing import List, Dict

engine_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if engine_dir not in sys.path:
    sys.path.insert(0, engine_dir)

from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator


def _run_producer_process(symbols: List[str], base_dir: str, cycles: int, batch_size: int, stop_event: mp.Event):
    """Isolated OS process for market order generation."""
    sim = MarketSimulator(symbols=symbols)
    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, create=False)
    try:
        for _ in range(cycles):
            if stop_event.is_set():
                break
            batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=batch_size)
            for sym, pkts in batch.items():
                if pkts:
                    hub.write_symbol_batch(sym, pkts)
            time.sleep(0.01)
    finally:
        hub.close()


def _run_consumer_process(symbols: List[str], base_dir: str, stop_event: mp.Event, result_dict: dict):
    """Isolated OS process for Cython matching engine order book draining."""
    from chronos_engine import OrderBook, RingConsumer

    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, create=False)
    books: Dict[str, OrderBook] = {}
    consumers: Dict[str, RingConsumer] = {}

    for sym in symbols:
        books[sym] = OrderBook(order_capacity=20000, level_capacity=500)
        consumers[sym] = RingConsumer(hub.ring_buffers[sym].filepath, hub.locks[sym].filepath)

    total_ingested = 0
    total_trades = 0

    try:
        while not stop_event.is_set():
            drained_any = False
            for sym in symbols:
                stat = consumers[sym].drain_into(books[sym])
                msgs = stat.get("messages", 0)
                if msgs > 0:
                    drained_any = True
                    total_ingested += msgs
                    total_trades += stat.get("trades", 0)
            if not drained_any:
                time.sleep(0.005)

        # Final drain after stop event signaled
        for sym in symbols:
            stat = consumers[sym].drain_into(books[sym])
            total_ingested += stat.get("messages", 0)
            total_trades += stat.get("trades", 0)

        result_dict["total_ingested"] = total_ingested
        result_dict["total_trades"] = total_trades
    finally:
        hub.close()


class MultiProcessCoordinator:
    """Orchestrates independent OS processes over shared-memory IPC rings."""

    def __init__(self, symbols: List[str], base_dir: str):
        self.symbols = symbols
        self.base_dir = base_dir

    def run_concurrent_session(self, cycles: int = 15, batch_size: int = 10) -> Dict[str, int]:
        stop_event = mp.Event()
        manager = mp.Manager()
        consumer_results = manager.dict()

        consumer_proc = mp.Process(
            target=_run_consumer_process,
            args=(self.symbols, self.base_dir, stop_event, consumer_results)
        )
        producer_proc = mp.Process(
            target=_run_producer_process,
            args=(self.symbols, self.base_dir, cycles, batch_size, stop_event)
        )

        consumer_proc.start()
        producer_proc.start()

        # Await producer completion
        producer_proc.join(timeout=10)
        time.sleep(0.2)  # Allow consumer to drain remaining items

        stop_event.set()
        consumer_proc.join(timeout=5)

        if producer_proc.is_alive():
            producer_proc.terminate()
        if consumer_proc.is_alive():
            consumer_proc.terminate()

        return dict(consumer_results)
