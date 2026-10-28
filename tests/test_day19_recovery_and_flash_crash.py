import os
import sys
import pytest

engine_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if engine_dir not in sys.path:
    sys.path.insert(0, engine_dir)

from chronos_engine import OrderBook, RingConsumer
from src.ring_buffer import MmapRingBuffer
from src.ipc_hub import MultiRingIPCHub
from src.market_feed import MarketSimulator
from src.wal import BinaryWALWriter
from src.ipc_recovery import IPCRecoveryManager

def test_day19_flash_crash_and_recovery(tmp_path):
    sym = "TSLA"
    ipc_dir = str(tmp_path / "ipc_d19")
    wal_file = str(tmp_path / "test_day19.wal")

    hub = MultiRingIPCHub(symbols=[sym], base_dir=ipc_dir, capacity_per_ring=512, create=True)
    wal_writer = BinaryWALWriter(wal_file)
    sim = MarketSimulator(symbols=[sym])
    book = OrderBook(order_capacity=10000, level_capacity=500)
    consumer = RingConsumer(hub.ring_buffers[sym].filepath, hub.locks[sym].filepath)

    try:
        # 1. Normal trading & Checkpointing at order sequence 10
        batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=10)[sym]
        wal_writer.append_batch(batch)
        hub.write_symbol_batch(sym, batch)
        drain_info = consumer.drain_into(book)
        assert drain_info["messages"] == 10

        wal_writer.create_checkpoint(checkpoint_seq=10)

        # 2. Member 3: Inject Flash Crash Cascade
        initial_px = sim.asset_prices[sym]
        panic_sells, bottom_bids, crashed_px = sim.generate_flash_crash_cascade(sym, crash_depth_pct=0.08)
        assert crashed_px < initial_px

        crash_stream = panic_sells + bottom_bids
        wal_writer.append_batch(crash_stream)
        hub.write_symbol_batch(sym, crash_stream)

        # 3. Simulate sudden ungraceful disconnect
        hub.ring_buffers[sym].close()
        wal_writer.close()

        # 4. Member 1: Replay unconsumed packets via IPCRecoveryManager
        recovery_mgr = IPCRecoveryManager(wal_file)
        recovered = recovery_mgr.recover_unprocessed_orders(symbol=sym, last_processed_seq=10)
        assert sym in recovered
        assert len(recovered[sym]) == len(crash_stream)

        # 5. Reattach consumer to the persistent ring and verify complete drain
        restored_ring = MmapRingBuffer(hub.ring_buffers[sym].filepath, capacity=512, create=False)
        recovered_consumer = RingConsumer(restored_ring.filepath, hub.locks[sym].filepath)
        summary = recovered_consumer.drain_into(book)
        assert summary["messages"] == len(crash_stream)

    finally:
        hub.close()
        hub.cleanup_files()
