import os
import struct
from typing import Dict, List, Tuple
from src.wal import BinaryWALReader
from src.schema import unpack_order
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock

class IPCRecoveryManager:
    """Day 19 Member 1: Reconstructs state after process crashes and reconciles ring buffers."""

    def __init__(self, wal_path: str, checkpoint_path: str = None):
        self.wal_path = wal_path
        self.checkpoint_path = checkpoint_path or f"{wal_path}.checkpoint"

    def read_last_checkpoint(self) -> Tuple[int, bytes]:
        if not os.path.exists(self.checkpoint_path):
            return 0, b""
        with open(self.checkpoint_path, "rb") as f:
            header = f.read(20)
            if len(header) < 16:
                return 0, b""
            magic, last_seq, snap_len = struct.unpack("<4sQI", header[:16])
            if magic != b"CKPT":
                return 0, b""
            snapshot = f.read(snap_len)
            return last_seq, snapshot

    def recover_unprocessed_orders(self, symbol: str = "TSLA", last_processed_seq: int = None) -> Dict[str, List[bytes]]:
        """Scans the WAL using unpack_order and collects orders stamped after last_processed_seq."""
        if last_processed_seq is None:
            last_processed_seq, _ = self.read_last_checkpoint()

        reader = BinaryWALReader(self.wal_path)
        recovered_by_symbol: Dict[str, List[bytes]] = {symbol: []}

        for raw_pkt in reader.replay_records():
            try:
                order = unpack_order(raw_pkt)
                if order.order_id > last_processed_seq:
                    recovered_by_symbol[symbol].append(raw_pkt)
            except Exception:
                continue

        return recovered_by_symbol

    def restore_ring_buffer(self, ring: MmapRingBuffer, lock: MmapProcessLock, packets: List[bytes]) -> int:
        """Injects recovered packets back into the ring buffer."""
        with lock:
            written = ring.write_batch(packets)
            return written
