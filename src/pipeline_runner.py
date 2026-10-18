import os
import sys
import time
import multiprocessing as mp
from typing import List, Dict

sys.path.insert(0, os.path.abspath("engine"))
sys.path.insert(0, os.path.abspath("."))

from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.schema import unpack_order
from chronos_engine import OrderBook


def producer_worker(symbols: List[str], base_dir: str, duration_sec: float, stop_event: mp.Event):
    """Producer Process: generates and writes continuous correlated market flow into symbol rings."""
    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, capacity_per_ring=4096, create=False)
    sim = MarketSimulator(symbols=symbols)

    for multi_batch in sim.stream_continuous_multi_asset(duration_sec=duration_sec, burst_interval_ms=20.0, batch_size_per_symbol=25):
        if stop_event.is_set():
            break
        for sym, packets in multi_batch.items():
            hub.write_symbol_batch(sym, packets)

    hub.close()


def consumer_worker(symbols: List[str], base_dir: str, stop_event: mp.Event, shared_counts: mp.Array):
    """Consumer Process: drains ring buffers in parallel and submits orders to Cython OrderBooks."""
    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, capacity_per_ring=4096, create=False)
    books: Dict[str, OrderBook] = {sym: OrderBook() for sym in symbols}

    local_consumed = 0
    local_matched = 0

    while not stop_event.is_set():
        data = hub.round_robin_read(max_count_per_symbol=50)
        has_data = False
        for sym, packets in data.items():
            if packets:
                has_data = True
                book = books[sym]
                for raw_pkt in packets:
                    order = unpack_order(raw_pkt)
                    local_consumed += 1
                    fills = book.submit(order.order_id, order.side, order.price, order.quantity)
                    local_matched += len(fills)

        if not has_data:
            time.sleep(0.005)

    # Final drain
    final_data = hub.round_robin_read(max_count_per_symbol=100)
    for sym, packets in final_data.items():
        book = books[sym]
        for raw_pkt in packets:
            order = unpack_order(raw_pkt)
            local_consumed += 1
            fills = book.submit(order.order_id, order.side, order.price, order.quantity)
            local_matched += len(fills)

    shared_counts[0] = local_consumed
    shared_counts[1] = local_matched
    hub.close()


def run_multiprocess_system(symbols: List[str], duration_sec: float = 1.5, base_dir: str = None) -> Dict[str, int]:
    """Orchestrates producer and consumer across independent OS processes."""
    import tempfile
    created_temp = False
    if base_dir is None:
        base_dir = tempfile.mkdtemp(prefix="chronos_mp_")
        created_temp = True

    # 1. Initialize master buffers
    master_hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, capacity_per_ring=4096, create=True)

    stop_event = mp.Event()
    shared_counts = mp.Array("i", [0, 0])  # [consumed, matched]

    p_consumer = mp.Process(target=consumer_worker, args=(symbols, base_dir, stop_event, shared_counts))
    p_producer = mp.Process(target=producer_worker, args=(symbols, base_dir, duration_sec, stop_event))

    p_consumer.start()
    p_producer.start()

    p_producer.join()
    time.sleep(0.2)  # allow consumer to drain in-flight ring items
    stop_event.set()
    p_consumer.join()

    master_hub.close()
    master_hub.cleanup_files()

    if created_temp:
        try:
            os.rmdir(base_dir)
        except OSError:
            pass

    return {"consumed": shared_counts[0], "matched": shared_counts[1]}


if __name__ == "__main__":
    res = run_multiprocess_system(symbols=["AAPL", "MSFT", "TSLA"], duration_sec=1.5)
    print(f"[+] Multi-process complete: Consumed={res['consumed']:,} Matched={res['matched']:,}")
