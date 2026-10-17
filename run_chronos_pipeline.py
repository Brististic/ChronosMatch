import os
import sys
import time
import tempfile
from typing import Dict

sys.path.insert(0, os.path.abspath("engine"))
sys.path.insert(0, os.path.abspath("."))

from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.schema import unpack_order
from chronos_engine import OrderBook
from ui.dashboard import MultiAssetOrderBookUI


def run_pipeline(iterations: int = 5, batch_size_per_symbol: int = 50):
    symbols = ["AAPL", "MSFT", "TSLA"]
    base_dir = tempfile.mkdtemp(prefix="chronos_master_")

    print("=================================================================")
    print("      CHRONOSMATCH DAY 8: MULTI-ASSET INTEGRATED PIPELINE        ")
    print("=================================================================")
    print(f"[*] Registered Symbols: {', '.join(symbols)}")
    print(f"[*] Base IPC Buffer Directory: {base_dir}")

    # 1. Initialize Multi-Ring IPC Hub (Member 1)
    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, capacity_per_ring=4096, create=True)

    # 2. Initialize Cython OrderBooks per symbol (Member 2)
    books: Dict[str, OrderBook] = {sym: OrderBook() for sym in symbols}

    # 3. Initialize Correlated Market Simulator (Member 3)
    sim = MarketSimulator(symbols=symbols)

    # 4. Initialize Multi-Asset Terminal UI Tracker (Member 4)
    ui = MultiAssetOrderBookUI(symbols=symbols)

    total_matches = 0
    total_orders = 0

    try:
        for step in range(1, iterations + 1):
            print(f"\n[>>>] Running Batch Iteration {step}/{iterations}...")

            # --- MEMBER 3: Generate Correlated Multi-Asset Flow ---
            multi_batch = sim.generate_correlated_multi_asset_batch(
                batch_size_per_symbol=batch_size_per_symbol,
                macro_market_drift=0.0005,
            )

            # --- MEMBER 1: Write Batches to Symbol-Dedicated Rings ---
            for sym, raw_packets in multi_batch.items():
                hub.write_symbol_batch(sym, raw_packets)

            # --- MEMBER 1: Fair Round-Robin Read Across Channels ---
            drained_data = hub.round_robin_read(max_count_per_symbol=batch_size_per_symbol)

            # --- MEMBER 2 & MEMBER 4: Cython Matching & UI Update ---
            now_ns = time.perf_counter_ns()
            for sym, packets in drained_data.items():
                engine_book = books[sym]

                for raw_pkt in packets:
                    order = unpack_order(raw_pkt)
                    total_orders += 1

                    # Microsecond transit latency
                    transit_us = max(0.0, (now_ns - order.timestamp_ns) / 1000.0)

                    # Match order in Cython engine
                    fills = engine_book.submit(order.order_id, order.side, order.price, order.quantity)
                    total_matches += len(fills)

                    # Update UI depth book & metrics
                    ui.update_symbol_order(
                        symbol=sym,
                        side=order.side,
                        price=order.price,
                        quantity=order.quantity,
                        latency_us=transit_us,
                    )

                # Verify Cython book integrity after every batch
                assert engine_book.check_integrity(), f"Book integrity failure in {sym}!"

            # Render updated UI snapshot
            print(ui.render_text_snapshot())
            time.sleep(0.05)

        print("\n=================================================================")
        print("                INTEGRATION EXECUTION SUMMARY                    ")
        print("=================================================================")
        print(f"    [+] Total Orders Ingested & Processed: {total_orders:,}")
        print(f"    [+] Total Crossing Matches Executed:   {total_matches:,}")
        for sym in symbols:
            m = ui.get_symbol_metrics(sym)
            print(f"    [+] {sym:<5} Mid: ${m['mid']:<7.2f} Spread: ${m['spread']:<5.2f} P50 Latency: {m['p50_latency_us']} us")
        print("=================================================================")
        print("STATUS: END-TO-END MULTI-ASSET INTEGRATION VERIFIED")
        print("=================================================================\n")

    finally:
        hub.close()
        hub.cleanup_files()
        try:
            os.rmdir(base_dir)
        except OSError:
            pass


if __name__ == "__main__":
    run_pipeline(iterations=4, batch_size_per_symbol=50)
