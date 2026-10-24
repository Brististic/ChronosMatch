import os
import sys
import time
import inspect
import tempfile
from typing import Dict, List, Set

sys.path.insert(0, os.path.abspath("engine"))
sys.path.insert(0, os.path.abspath("."))

from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.schema import pack_order, unpack_order
from src.wal import BinaryWALWriter
from src.ring_telemetry import RingTelemetryMonitor
from chronos_engine import OrderBook
from ui.dashboard import MultiAssetOrderBookUI


def set_sim_regime(sim: MarketSimulator, regime: str):
    """Safely toggles the market regime across simulator versions."""
    if hasattr(sim, "set_market_regime"):
        sim.set_market_regime(regime)
    elif hasattr(sim, "market_regime"):
        sim.market_regime = regime
    elif hasattr(sim, "regime"):
        sim.regime = regime
    elif hasattr(sim, "current_regime"):
        sim.current_regime = regime


def call_iceberg_slice(sim: MarketSimulator, symbol: str, side: bytes, total_qty: int, count: int) -> List[bytes]:
    """Dynamically invokes iceberg slicing to match internal parameter naming."""
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


def generate_single_symbol_ticks(sim: MarketSimulator, symbol: str, count: int) -> List[bytes]:
    """Generates ticks for a single symbol dynamically adapting to method signatures."""
    if hasattr(sim, "generate_stress_burst"):
        try:
            return sim.generate_stress_burst(symbol=symbol, count=count)
        except TypeError:
            return sim.generate_stress_burst(symbol, count)
    
    # Fallback to general tick generator
    all_ticks = sim.generate_ticks(count=count)
    return all_ticks


def run_pipeline(iterations: int = 5, batch_size_per_symbol: int = 60):
    symbols = ["AAPL", "MSFT", "TSLA"]
    base_dir = tempfile.mkdtemp(prefix="chronos_master_")
    wal_path = os.path.join(base_dir, "audit_stream.wal")

    print("=================================================================")
    print("      CHRONOSMATCH DAY 14: HIGH-FREQUENCY INTEGRATED SYSTEM      ")
    print("=================================================================")
    print(f"[*] Registered Symbols: {', '.join(symbols)}")
    print(f"[*] Base IPC Buffer Directory: {base_dir}")
    print(f"[*] Binary WAL Disk Persistence: {wal_path}")

    # 1. Multi-Ring IPC Hub with Dynamic Backpressure (Member 1)
    hub = MultiRingIPCHub(
        symbols=symbols,
        base_dir=base_dir,
        capacity_per_ring=512,
        create=True,
        high_watermark=0.85
    )

    # 2. Binary Write-Ahead Log Writer (Member 1)
    wal_writer = BinaryWALWriter(wal_path)

    # 3. Telemetry Monitors for Ring Occupancy and Drops (Member 1)
    telemetry_monitors = {
        sym: RingTelemetryMonitor(hub.ring_buffers[sym], hub.locks[sym])
        for sym in symbols
    }

    # 4. Cython OrderBooks and Active ID tracking per Symbol (Member 2)
    books: Dict[str, OrderBook] = {sym: OrderBook() for sym in symbols}
    active_order_ids: Dict[str, Set[int]] = {sym: set() for sym in symbols}

    # 5. Multi-Regime Market Simulator (Member 3)
    sim = MarketSimulator(symbols=symbols)

    # 6. Terminal UI Depth and Latency Monitor (Member 4)
    ui = MultiAssetOrderBookUI(symbols=symbols)

    total_matches = 0
    total_orders = 0
    total_wal_logged = 0

    try:
        for step in range(1, iterations + 1):
            print(f"\n[>>>] Running Iteration {step}/{iterations}...")
            multi_batch: Dict[str, List[bytes]] = {sym: [] for sym in symbols}

            # --- MEMBER 3: Market Feed Stage Selection ---
            if step == 1:
                print("    [Flow] Correlated Multi-Asset Continuous Flow")
                multi_batch = sim.generate_correlated_multi_asset_batch(
                    batch_size_per_symbol=batch_size_per_symbol,
                    macro_market_drift=0.0008,
                )

            elif step == 2:
                print("    [Flow] High-Volatility Regime Switching (TURBULENT) & Jitter")
                set_sim_regime(sim, "TURBULENT")
                for sym in symbols:
                    jittered_pkts = sim.generate_jittered_burst(symbol=sym, count=batch_size_per_symbol, max_jitter_us=120)
                    multi_batch[sym].extend(jittered_pkts)

            elif step == 3:
                print("    [Flow] Institutional Iceberg Order Slicing")
                for sym in symbols:
                    clips = call_iceberg_slice(
                        sim=sim,
                        symbol=sym,
                        side=b"B" if step % 2 == 0 else b"S",
                        total_qty=1200,
                        count=batch_size_per_symbol,
                    )
                    multi_batch[sym].extend(clips)

            elif step == 4:
                print("    [Flow] Level-3 Granular Lifecycle Stream (ADD, MODIFY, CANCEL, EXEC)")
                for sym in symbols:
                    l3_events = sim.generate_l3_lifecycle_stream(symbol=sym, events_count=batch_size_per_symbol)
                    multi_batch[sym].extend([pkt for _, pkt in l3_events])

            elif step == 5:
                print("    [Flow] Cross-Asset Statistical Arbitrage Pair Bursts")
                arb_pairs = sim.generate_arbitrage_pair_burst("AAPL", "MSFT", spread_threshold=0.25)
                for sym in ("AAPL", "MSFT"):
                    multi_batch[sym].extend(arb_pairs.get(sym, []))
                
                # Dynamic safe generation for TSLA
                tsla_pkts = generate_single_symbol_ticks(sim, "TSLA", count=batch_size_per_symbol)
                multi_batch["TSLA"].extend(tsla_pkts)

            # --- MEMBER 1: WAL Disk Persistence & Dynamic Backpressure Ingestion ---
            for sym, raw_packets in multi_batch.items():
                if not raw_packets:
                    continue
                wal_writer.append_batch(raw_packets)
                total_wal_logged += len(raw_packets)
                hub.write_symbol_batch_with_backpressure(sym, raw_packets)

            # --- MEMBER 1: Round-Robin Extraction ---
            drained_data = hub.round_robin_read(max_count_per_symbol=batch_size_per_symbol * 2)

            # --- MEMBER 2 & MEMBER 4: Engine Matching & UI Depth Updates ---
            now_ns = time.perf_counter_ns()
            for sym, packets in drained_data.items():
                engine_book = books[sym]
                book_ids = active_order_ids[sym]

                for raw_pkt in packets:
                    order = unpack_order(raw_pkt)
                    total_orders += 1
                    transit_us = max(0.0, (now_ns - order.timestamp_ns) / 1000.0)

                    # Handle L3 Order Lifecycle Transitions
                    if order.order_id in book_ids:
                        if order.quantity == 0:
                            engine_book.cancel(order.order_id)
                            book_ids.remove(order.order_id)
                        else:
                            engine_book.cancel(order.order_id)
                            fills = engine_book.submit(order.order_id, order.side, order.price, order.quantity)
                            total_matches += len(fills)
                    else:
                        if order.quantity > 0:
                            fills = engine_book.submit(order.order_id, order.side, order.price, order.quantity)
                            book_ids.add(order.order_id)
                            total_matches += len(fills)

                    ui.update_symbol_order(
                        symbol=sym,
                        side=order.side,
                        price=order.price,
                        quantity=order.quantity,
                        latency_us=transit_us,
                    )

                assert engine_book.check_integrity(), f"Book integrity failure in {sym}!"

                # Member 1: Day 14 In-Memory Zero-Copy Ring Compaction
                if step == 3:
                    rb = hub.ring_buffers[sym]
                    lock = hub.locks[sym]
                    rb.compact_ring_buffer(lock)

            print(ui.render_text_snapshot())
            time.sleep(0.05)

        wal_writer.sync()

        print("\n=================================================================")
        print("                INTEGRATION EXECUTION SUMMARY                    ")
        print("=================================================================")
        print(f"    [+] Total Orders Ingested & Matched: {total_orders:,}")
        print(f"    [+] Total Crossing Matches Executed: {total_matches:,}")
        print(f"    [+] Total Records Synced to WAL:     {total_wal_logged:,}")
        for sym in symbols:
            m = ui.get_symbol_metrics(sym)
            telemetry_stat = telemetry_monitors[sym].sample_health() if hasattr(telemetry_monitors[sym], "sample_health") else {}
            occ_pct = telemetry_stat.get("occupancy_pct", hub.get_ring_occupancy(sym) * 100.0)
            drops_cnt = telemetry_stat.get("drops", 0)
            print(
                f"    [+] {sym:<5} Mid: ${m['mid']:<7.2f} Spread: ${m['spread']:<5.2f} "
                f"P50: {m['p50_latency_us']:<5.1f}us P99: {m.get('p99_latency_us', 0.0):<5.1f}us "
                f"Ring Occ: {occ_pct:.1f}% Drops: {drops_cnt}"
            )
        print("=================================================================")
        print("STATUS: DAY 14 END-TO-END MULTI-ASSET INTEGRATION VERIFIED")
        print("=================================================================\n")

    finally:
        wal_writer.close()
        hub.close()
        hub.cleanup_files()
        try:
            if os.path.exists(wal_path):
                os.remove(wal_path)
            os.rmdir(base_dir)
        except OSError:
            pass


if __name__ == "__main__":
    run_pipeline(iterations=5, batch_size_per_symbol=60)
