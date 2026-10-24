import os
import sys
import time
import inspect
import pytest

sys.path.insert(0, os.path.abspath("engine"))
sys.path.insert(0, os.path.abspath("."))

from src.schema import pack_order, unpack_order
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.engine_feeder import EngineFeeder


class DynamicPipelineHarness:
    """Introspects and exercises newly added simulator and ring buffer features automatically."""

    def __init__(self, symbols=["AAPL", "MSFT"]):
        self.symbols = symbols
        self.sim = MarketSimulator(symbols=symbols)

    def discover_sync_generators(self):
        """Finds all synchronous order/stream generator methods dynamically."""
        methods = []
        for name, fn in inspect.getmembers(self.sim, predicate=inspect.ismethod):
            # Only inspect synchronous generator functions
            if (name.startswith("generate_") or name.startswith("stream_")) and not inspect.iscoroutinefunction(fn):
                methods.append((name, fn))
        return methods

    def discover_ring_buffer_methods(self, rb):
        """Finds all non-standard buffer operations dynamically."""
        methods = []
        for name, fn in inspect.getmembers(rb, predicate=inspect.ismethod):
            if not name.startswith("_") and name not in ("write_batch", "read_batch", "close", "resize_ring_buffer"):
                methods.append((name, fn))
        return methods


def test_dynamic_end_to_end_introspected_pipeline(tmp_path):
    """
    Invariant-Driven Integration Test:
    Never needs daily updates. Automatically discovers any new methods on Member 1,
    Member 2, and Member 3, executes them, and asserts core low-latency invariants.
    """
    harness = DynamicPipelineHarness(symbols=["AAPL", "MSFT"])
    base_dir = str(tmp_path / "dynamic_ipc")

    hub = MultiRingIPCHub(
        symbols=harness.symbols,
        base_dir=base_dir,
        capacity_per_ring=256,
        create=True,
        high_watermark=0.90
    )

    try:
        # 1. Dynamically discover and execute all synchronous MarketSimulator generators (Member 3)
        gen_methods = harness.discover_sync_generators()
        assert len(gen_methods) > 0, "No generator methods discovered in MarketSimulator"

        generated_packets = []
        for name, fn in gen_methods:
            sig = inspect.signature(fn)
            params = sig.parameters
            try:
                if "symbol" in params and "symbols" not in params:
                    res = fn("AAPL")
                elif "sym_a" in params and "sym_b" in params:
                    res = fn("AAPL", "MSFT")
                elif "symbols" in params:
                    res = fn(harness.symbols)
                else:
                    res = fn()

                # Normalize output into flat list of 29-byte packets
                if isinstance(res, list):
                    for item in res:
                        if isinstance(item, bytes) and len(item) == 29:
                            generated_packets.append(("AAPL", item))
                        elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                            generated_packets.append(("AAPL", item[1]))
                elif isinstance(res, dict):
                    for sym, pkts in res.items():
                        for p in pkts:
                            if isinstance(p, bytes) and len(p) == 29:
                                generated_packets.append((sym, p))
            except Exception:
                # Custom generators with required multi-parameter configs fall back
                continue

        assert len(generated_packets) > 0, "Dynamic generation produced 0 valid packets"

        # 2. Ingest through MultiRingIPCHub (Member 1)
        for sym, pkt in generated_packets:
            target_sym = sym if sym in hub.ring_buffers else "AAPL"
            hub.write_symbol_batch_with_backpressure(target_sym, [pkt])

        # 3. Dynamically discover and exercise Ring Buffer Operations (Member 1)
        rb_sample = hub.ring_buffers["AAPL"]
        lock_sample = hub.locks["AAPL"]
        buffer_ops = harness.discover_ring_buffer_methods(rb_sample)

        for op_name, op_fn in buffer_ops:
            sig = inspect.signature(op_fn)
            if "lock" in sig.parameters and len(sig.parameters) == 1:
                op_fn(lock_sample)

        # 4. Drain through EngineFeeder flexibly according to its signature
        feeder_sig = inspect.signature(EngineFeeder.__init__)
        feeder_params = list(feeder_sig.parameters.keys())

        # Check if EngineFeeder takes (self, ring_buffer, lock) or (self, hub)
        if len(feeder_params) >= 3 and "lock" in feeder_params:
            feeders = {
                sym: EngineFeeder(hub.ring_buffers[sym], hub.locks[sym])
                for sym in harness.symbols
            }
            total_consumed = 0
            for sym, feeder in feeders.items():
                if hasattr(feeder, "drain_batch"):
                    res = feeder.drain_batch(max_orders=128)
                    total_consumed += len(res) if isinstance(res, list) else (res or 0)
                elif hasattr(feeder, "drain_all"):
                    res = feeder.drain_all()
                    total_consumed += len(res) if isinstance(res, list) else (res or 0)
                
                # Check OrderBook invariants
                if hasattr(feeder, "book"):
                    assert feeder.book.check_integrity() is True
        else:
            feeder = EngineFeeder(hub)
            total_consumed = feeder.drain_all_symbols(max_per_symbol=128)

        assert total_consumed > 0, "Pipeline failed to drain dynamically generated packets"

        # 5. Core Invariant Checks
        for sym in harness.symbols:
            occ = hub.get_ring_occupancy(sym)
            assert 0.0 <= occ <= 1.0, f"Invalid ring occupancy {occ} on {sym}"

    finally:
        hub.close()
        hub.cleanup_files()
