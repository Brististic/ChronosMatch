# -*- coding: utf-8 -*-
import os
import shutil
import pytest
from src.ipc_hub import MultiRingIPCHub
from src.multiprocess_runner import MultiProcessCoordinator


def test_day21_multiprocess_isolated_execution(tmp_path):
    symbols = ["AAPL", "MSFT"]
    ipc_dir = str(tmp_path / "mp_ipc")
    os.makedirs(ipc_dir, exist_ok=True)

    # Initialize shared memory files
    hub = MultiRingIPCHub(symbols=symbols, base_dir=ipc_dir, capacity_per_ring=512, create=True)
    hub.close()

    coordinator = MultiProcessCoordinator(symbols=symbols, base_dir=ipc_dir)
    results = coordinator.run_concurrent_session(cycles=10, batch_size=8)

    assert "total_ingested" in results
    assert results["total_ingested"] > 0
    # 2 symbols * 10 cycles * 8 orders = 160 generated orders
    assert results["total_ingested"] >= 100
