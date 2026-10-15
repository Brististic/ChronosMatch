import os
import pytest
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.market_feed import MarketSimulator
from src.schema import unpack_order


@pytest.fixture
def stress_env(tmp_path):
    buf_path = str(tmp_path / "stress_ring.bin")
    lock_path = str(tmp_path / "stress.lock")
    rb = MmapRingBuffer(buf_path, capacity=100, create=True)
    lock = MmapProcessLock(lock_path, create=True)

    yield rb, lock

    rb.close()
    lock.close()


def test_buffer_integrity_audit(stress_env):
    rb, _ = stress_env
    assert rb.verify_integrity() is True


def test_dead_pid_lock_reclaim(tmp_path):
    lock_path = str(tmp_path / "reclaim.lock")
    lock = MmapProcessLock(lock_path, create=True)

    lock._write_state(1, 9999999)
    flag, owner = lock._read_state()
    assert flag == 1
    assert owner == 9999999

    new_lock = MmapProcessLock(lock_path, create=False)
    acquired = new_lock.acquire(timeout_sec=0.5)
    assert acquired is True
    
    flag, owner = new_lock._read_state()
    assert flag == 1
    assert owner == os.getpid()

    new_lock.release()
    lock.close()
    new_lock.close()


@pytest.mark.asyncio
async def test_flash_crash_simulation():
    sim = MarketSimulator(symbols=["AAPL"])
    initial_price = sim.asset_prices["AAPL"]

    crash_packets = await sim.generate_flash_crash_sequence(
        "AAPL", steps=50, crash_drop_pct=0.20, panic_cancel_rate=0.40
    )
    assert len(crash_packets) == 50

    orders = [unpack_order(p) for p in crash_packets]
    final_price = sim.asset_prices["AAPL"]

    assert final_price < initial_price
    assert any(o.side == b"C" for o in orders)