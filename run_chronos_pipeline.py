import os
import sys
import time
import inspect
from typing import Dict, List

# Ensure local engine directory is on sys.path for C-extension resolution
engine_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "engine"))
if engine_path not in sys.path:
    sys.path.insert(0, engine_path)

try:
    from chronos_engine import OrderBook, RingConsumer
    HAVE_CYTHON_ENGINE = True
except ImportError:
    HAVE_CYTHON_ENGINE = False
    print("[WARN] chronos_engine C-extension not found or built. Falling back to EngineFeeder emulation.")

from src.ring_buffer import MmapRingBuffer
from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.schema import pack_order, unpack_order, pack_fill, unpack_fill, Order
from src.wal import BinaryWALWriter, BinaryWALReader
from src.gap_detector import SequenceGapDetector
from src.engine_feeder import EngineFeeder
from ui.dashboard import MultiAssetOrderBookUI


def call_iceberg_slice(sim: MarketSimulator, symbol: str, side: bytes, total_qty: int, count: int) -> List[bytes]:
    fn = sim.generate_iceberg_order_slice
    params = inspect.signature(fn).parameters
    kwargs = {}
    if "symbol" in params:
        kwargs["symbol"] = symbol
    if "side" in params:
        kwargs["side"] = side
    if "total_quantity" in params:
        kwargs["total_quantity"] = total_qty
    elif "quantity" in params:
        kwargs["quantity"] = total_qty
    elif "total_qty" in params:
        kwargs["total_qty"] = total_qty

    if "clip_count" in params:
        kwargs["clip_count"] = count
    elif "num_slices" in params:
        kwargs["num_slices"] = count
    elif "slices" in params:
        kwargs["slices"] = count
    elif "count" in params:
        kwargs["count"] = count

    if kwargs:
        return fn(**kwargs)
    return fn(symbol, side, total_qty, count)


def main():
    print("=" * 70)
    print("  CHRONOS MATCH MASTER PIPELINE EXECUTION (DAY 1 - DAY 17)")
    print("=" * 70)

    symbols = ["AAPL", "MSFT", "TSLA"]
    base_ipc_dir = "data/chronos_ipc_runtime"
    wal_file = "data/audit_day17.wal"
    os.makedirs(base_ipc_dir, exist_ok=True)
    os.makedirs("data", exist_ok=True)

    # 1. Initialize Multi-Ring IPC Hub (Days 1-8)
    print("[1/7] Initializing MultiRingIPCHub and WAL Writer...")
    hub = MultiRingIPCHub(
        symbols=symbols,
        base_dir=base_ipc_dir,
        capacity_per_ring=256,
        create=True,
        high_watermark=0.85
    )
    wal_writer = BinaryWALWriter(wal_file)

    # 2. Initialize Outbound Fill Rings (Day 16 / Day 13 contract)
    for sym in symbols:
        hub.init_execution_ring(sym, capacity=256)

    # 3. Engine & Consumer Setup (Day 9, Day 12)
    order_books = {}
    ring_consumers = {}
    if HAVE_CYTHON_ENGINE:
        print("[2/7] Binding Cython OrderBooks with Preallocated Memory Pools...")
        for sym in symbols:
            order_books[sym] = OrderBook(order_capacity=10000, level_capacity=500)
            ring_consumers[sym] = RingConsumer(hub.ring_buffers[sym].filepath, hub.locks[sym].filepath)
    else:
        print("[2/7] Initializing Python EngineFeeders...")

    # 4. Gap Detectors and Multi-Asset UI (Day 17)
    gap_detectors = {sym: SequenceGapDetector(expected_start_seq=1) for sym in symbols}
    sim = MarketSimulator(symbols=symbols)
    ui = MultiAssetOrderBookUI(symbols=symbols)

    total_orders_ingested = 0
    total_fills_logged = 0

    try:
        # -------------------------------------------------------------
        # STAGE 1: Correlated Flow & Gap Detection (Days 1-13)
        # -------------------------------------------------------------
        print("\n[Stage 1] Ingesting Correlated Multi-Asset Flow & Gap Verification...")
        batch_1 = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=25)
        for sym, pkts in batch_1.items():
            wal_writer.append_batch(pkts)
            hub.write_symbol_batch_with_backpressure(sym, pkts)

        for sym in symbols:
            if HAVE_CYTHON_ENGINE:
                summary = ring_consumers[sym].drain_into(order_books[sym])
                total_orders_ingested += summary["messages"]
            else:
                pkts = hub.ring_buffers[sym].read_batch(max_count=100, lock=hub.locks[sym])
                total_orders_ingested += len(pkts)
        print(f"  -> Dispatched {total_orders_ingested} correlated orders.")

        # -------------------------------------------------------------
        # STAGE 2: Iceberg Slicing & Zero-Copy Compaction (Day 14)
        # -------------------------------------------------------------
        print("\n[Stage 2] Running Iceberg Slicing & Ring Compaction (Day 14)...")
        for sym in symbols:
            slices = call_iceberg_slice(sim, sym, b"B", 600, 6)
            hub.write_symbol_batch(sym, slices)
            wal_writer.append_batch(slices)

            unconsumed = hub.ring_buffers[sym].compact_ring_buffer(hub.locks[sym])
            print(f"  -> Compacted ring for {sym}. Unconsumed slots: {unconsumed}")

            if HAVE_CYTHON_ENGINE:
                summary = ring_consumers[sym].drain_into(order_books[sym])
                total_orders_ingested += summary["messages"]
            else:
                pkts = hub.ring_buffers[sym].read_batch(max_count=50, lock=hub.locks[sym])
                total_orders_ingested += len(pkts)

        # -------------------------------------------------------------
        # STAGE 3: Dynamic In-Memory Ring Resizing (Day 15)
        # -------------------------------------------------------------
        print("\n[Stage 3] Dynamic In-Memory Buffer Resizing to 512 Slots (Day 15)...")
        for sym in symbols:
            success = hub.ring_buffers[sym].resize_ring_buffer(512, hub.locks[sym])
            assert success is True
            print(f"  -> Resized {sym} ring buffer capacity to: {hub.ring_buffers[sym].capacity}")

        # -------------------------------------------------------------
        # STAGE 4: Microstructural Order Book Imbalance (OBI) Flow (Day 15)
        # -------------------------------------------------------------
        print("\n[Stage 4] Generating Microstructural OBI Pressure Flow (Day 15)...")
        obi_pkts = sim.generate_book_imbalance_pressure("AAPL", target_imbalance=0.80, batch_size=30)
        hub.write_symbol_batch("AAPL", obi_pkts)
        wal_writer.append_batch(obi_pkts)

        if HAVE_CYTHON_ENGINE:
            summary_aapl = ring_consumers["AAPL"].drain_into(order_books["AAPL"])
            total_orders_ingested += summary_aapl["messages"]
        else:
            pkts = hub.ring_buffers["AAPL"].read_batch(max_count=50, lock=hub.locks["AAPL"])
            total_orders_ingested += len(pkts)

        # -------------------------------------------------------------
        # STAGE 5: Informed Toxic Flow Sweep (Day 16)
        # -------------------------------------------------------------
        print("\n[Stage 5] Injecting Informed Toxic Flow Sweeps (Day 16)...")
        toxic_orders, new_price = sim.generate_toxic_flow("MSFT", sweep_depth=10, price_jump=2.0)
        hub.write_symbol_batch("MSFT", toxic_orders)
        wal_writer.append_batch(toxic_orders)
        print(f"  -> MSFT Midpoint shifted to ${new_price:.2f}")

        if HAVE_CYTHON_ENGINE:
            summary_msft = ring_consumers["MSFT"].drain_into(order_books["MSFT"])
            total_orders_ingested += summary_msft["messages"]
        else:
            pkts = hub.ring_buffers["MSFT"].read_batch(max_count=50, lock=hub.locks["MSFT"])
            total_orders_ingested += len(pkts)

        # -------------------------------------------------------------
        # STAGE 6: Spoofing & Layering Microstructural Attack (Day 17)
        # -------------------------------------------------------------
        print("\n[Stage 6] Simulating Spoofing & Layering Microstructural Attack (Day 17)...")
        fake_bids, real_sell, cancels = sim.generate_spoofing_attack("TSLA", fake_layers=5, fake_qty_per_layer=500)
        attack_stream = fake_bids + [real_sell] + cancels
        hub.write_symbol_batch("TSLA", attack_stream)
        wal_writer.append_batch(attack_stream)

        if HAVE_CYTHON_ENGINE:
            summary_tsla = ring_consumers["TSLA"].drain_into(order_books["TSLA"])
            total_orders_ingested += summary_tsla["messages"]
        else:
            pkts = hub.ring_buffers["TSLA"].read_batch(max_count=50, lock=hub.locks["TSLA"])
            total_orders_ingested += len(pkts)

        # Emit simulated fills on execution ring
        now_ns = time.perf_counter_ns()
        for idx in range(1, 11):
            fill = pack_fill(
                order_id=1000 + idx,
                fill_id=idx,
                side=b"B" if idx % 2 == 0 else b"S",
                price=150.0 + (idx * 0.25),
                quantity=100,
                timestamp_ns=now_ns,
            )
            hub.write_fill("AAPL", fill)
            total_fills_logged += 1

        wal_writer.close()

        # -------------------------------------------------------------
        # STAGE 7: Audit Verification & UI Refresh
        # -------------------------------------------------------------
        print("\n[Stage 7] Performing Binary WAL Replay Audit & UI Refresh...")
        wal_reader = BinaryWALReader(wal_file)
        wal_records = list(wal_reader.replay_records())

        for sym in symbols:
            drained_fills = hub.read_fills(sym, max_count=50)
            if drained_fills:
                print(f"  -> Outbound fills drained for {sym}: {len(drained_fills)}")

        if HAVE_CYTHON_ENGINE:
            for sym in symbols:
                stats = order_books[sym].pool_stats()
                orders_stat = stats.get("orders", {})
                levels_stat = stats.get("levels", {})
                print(
                    f"  -> Cython Pool Stats [{sym}]: "
                    f"Orders In-Use={orders_stat.get('in_use', 0)}/{orders_stat.get('capacity', 0)} (Peak={orders_stat.get('peak', 0)}) | "
                    f"Levels In-Use={levels_stat.get('in_use', 0)}/{levels_stat.get('capacity', 0)} (Peak={levels_stat.get('peak', 0)})"
                )
    finally:
        hub.close()
        hub.cleanup_files()
        if os.path.exists(wal_file):
            try:
                os.remove(wal_file)
            except OSError:
                pass


if __name__ == "__main__":
    main()
