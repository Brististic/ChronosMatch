"""
ChronosMatch - Day 19 Complete Integration Pipeline Runner
Wires together:
  - Member 1: Multi-Ring IPC Hub, Binary WAL persistence & checkpointing, IPCRecoveryManager replay
  - Member 2: Cython OrderBook, RingConsumer zero-copy batch drain, fast-path O(1) cancel & pool recycling
  - Member 3: MarketSimulator multi-asset batches, quote floods & flash crash cascade generation
"""

import os
import sys
import time
import shutil
from typing import Dict, List

engine_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "engine"))
if engine_dir not in sys.path:
    sys.path.insert(0, engine_dir)

from chronos_engine import OrderBook, RingConsumer
from src.ipc_hub import MultiRingIPCHub
from src.ring_buffer import MmapRingBuffer
from src.wal import BinaryWALWriter
from src.ipc_recovery import IPCRecoveryManager
from src.market_feed import MarketSimulator
from src.schema import pack_order


def print_banner(msg: str):
    print(f"\n{'=' * 65}\n  {msg}\n{'=' * 65}")


def run_pipeline(symbols: List[str] = None, cycles: int = 5):
    symbols = symbols or ["AAPL", "MSFT", "TSLA"]
    base_ipc_dir = os.path.abspath("runtime_ipc")
    wal_dir = os.path.abspath("runtime_wal")

    for d in (base_ipc_dir, wal_dir):
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    print_banner("CHRONOSMATCH PIPELINE INTEGRATION (THROUGH DAY 19)")
    print(f"[*] Active Assets   : {', '.join(symbols)}")
    print(f"[*] IPC Directory   : {base_ipc_dir}")
    print(f"[*] WAL Directory   : {wal_dir}")

    hub = MultiRingIPCHub(
        symbols=symbols,
        base_dir=base_ipc_dir,
        capacity_per_ring=1024,
        create=True
    )

    books: Dict[str, OrderBook] = {}
    consumers: Dict[str, RingConsumer] = {}
    wal_writers: Dict[str, BinaryWALWriter] = {}
    outbound_fill_rings: Dict[str, MmapRingBuffer] = {}
    sim = MarketSimulator(symbols=symbols)

    total_orders_generated = 0
    total_matches_executed = 0

    try:
        # 1. Initialize per-symbol engines, rings, consumers, and loggers
        for sym in symbols:
            books[sym] = OrderBook(order_capacity=20000, level_capacity=500)
            consumers[sym] = RingConsumer(
                hub.ring_buffers[sym].filepath,
                hub.locks[sym].filepath
            )
            wal_writers[sym] = BinaryWALWriter(os.path.join(wal_dir, f"{sym}.wal"))

            fill_ring_path = os.path.join(base_ipc_dir, f"fills_{sym}.bin")
            outbound_fill_rings[sym] = MmapRingBuffer(
                fill_ring_path,
                capacity=1024,
                slot_size=53,
                create=True
            )

        print("\n[>>>] Commencing Live Ingestion & Matching Cycles...")

        # 2. Normal Ingestion & Matching Cycles
        for cycle in range(1, cycles + 1):
            print(f"\n--- [Cycle {cycle}/{cycles}] ---")

            feed_batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=15)

            for sym in symbols:
                pkts = feed_batch.get(sym, [])
                total_orders_generated += len(pkts)

                wal_writers[sym].append_batch(pkts)
                hub.write_symbol_batch(sym, pkts)

                drain_stat = consumers[sym].drain_into(books[sym])
                total_matches_executed += drain_stat.get("trades", 0)

                best_bid = books[sym].best_bid()
                best_ask = books[sym].best_ask()
                bid_px = best_bid[0] if isinstance(best_bid, tuple) else best_bid
                ask_px = best_ask[0] if isinstance(best_ask, tuple) else best_ask
                stats = books[sym].pool_stats()["orders"]

                print(
                    f"  [{sym}] Ingested: {drain_stat['messages']:2d} | "
                    f"Bid: {str(bid_px):>7} | Ask: {str(ask_px):>7} | "
                    f"Pool In-Use: {stats['in_use']:3d}/{stats['capacity']}"
                )

            # Day 19: Checkpoint WAL at Cycle 3
            if cycle == 3:
                for sym in symbols:
                    wal_writers[sym].create_checkpoint(checkpoint_seq=total_orders_generated)
                print("  [CKPT] Binary WAL checkpoints created across all assets.")

            # Fast-Path O(1) Cancellation verification
            if cycle % 2 == 0:
                cancel_sym = symbols[0]
                test_cancel_id = 999000 + cycle
                cancel_pkt = pack_order(test_cancel_id, b"B", 100.0, 50, time.time_ns())
                hub.write_symbol_batch(cancel_sym, [cancel_pkt])
                consumers[cancel_sym].drain_into(books[cancel_sym])

                ok = books[cancel_sym].cancel(test_cancel_id)
                print(f"  [CAN] Direct Cancel order_id={test_cancel_id} on {cancel_sym}: {'SUCCESS' if ok else 'FAILED'}")

            time.sleep(0.05)

        # 3. Day 19 Member 3: Inject Flash Crash Cascade on TSLA
        print_banner("DAY 19 STRESS TEST: INJECTING FLASH CRASH CASCADE")
        crash_sym = "TSLA"
        pre_crash_px = sim.asset_prices[crash_sym]
        sells, bids, crashed_px = sim.generate_flash_crash_cascade(crash_sym, crash_depth_pct=0.08)
        print(f"[*] Triggered flash crash on {crash_sym}: ${pre_crash_px:.2f} -> ${crashed_px:.2f}")

        crash_stream = sells + bids
        wal_writers[crash_sym].append_batch(crash_stream)
        hub.write_symbol_batch(crash_sym, crash_stream)
        total_orders_generated += len(crash_stream)

        drain_crash = consumers[crash_sym].drain_into(books[crash_sym])
        print(f"[*] Ingested {drain_crash['messages']} flash crash orders into {crash_sym} OrderBook.")

        # 4. Day 19 Member 1: Verify IPC Recovery Replay
        print_banner("DAY 19 DURABILITY: IPC RECOVERY MANAGER REPLAY")
        wal_file = os.path.join(wal_dir, f"{crash_sym}.wal")
        recovery_mgr = IPCRecoveryManager(wal_file)
        ckpt_seq, _ = recovery_mgr.read_last_checkpoint()
        recovered = recovery_mgr.recover_unprocessed_orders(symbol=crash_sym, last_processed_seq=ckpt_seq)
        recovered_count = len(recovered.get(crash_sym, []))
        print(f"[*] Last Checkpoint Sequence: {ckpt_seq}")
        print(f"[*] Recovered Unprocessed Orders from WAL: {recovered_count} orders for {crash_sym}")
        assert recovered_count > 0, "Recovery Manager failed to replay post-checkpoint orders!"

        # 5. Telemetry & Summary
        print_banner("PIPELINE EXECUTION SUMMARY")
        print(f"Total Orders Ingested      : {total_orders_generated}")
        print(f"Total Matches Executed     : {total_matches_executed}")

        for sym in symbols:
            pool = books[sym].pool_stats()
            orders_info = pool.get("orders", {})
            levels_info = pool.get("levels", {})
            in_use = orders_info.get("in_use", 0)
            cap = orders_info.get("capacity", 20000)
            lvl_in_use = levels_info.get("in_use", 0)
            print(
                f"[{sym}] Orders In-Use: {in_use:4d} / {cap:5d} | "
                f"Levels In-Use: {lvl_in_use:3d}"
            )

        print("\n[OK] Pipeline integration run through Day 19 completed successfully!")

    finally:
        print("\n[*] Tearing down IPC buffers and file handles...")
        hub.close()
        for sym in symbols:
            outbound_fill_rings[sym].close()
            wal_writers[sym].close()
        hub.cleanup_files()
        shutil.rmtree(base_ipc_dir, ignore_errors=True)
        shutil.rmtree(wal_dir, ignore_errors=True)


if __name__ == "__main__":
    run_pipeline()
