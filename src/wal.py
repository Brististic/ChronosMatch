import os
import struct
import zlib
from typing import List, Generator, Tuple

# WAL Record Frame Layout:
# Magic: 0x5741 (2 bytes) | Sequence: uint64 (8 bytes) | Length: uint16 (2 bytes)
# Payload: 29 bytes | Checksum: uint32 (4 bytes) -> Total Frame = 45 bytes
WAL_HEADER_FORMAT = "<HQH"
WAL_HEADER_SIZE = struct.calcsize(WAL_HEADER_FORMAT)
WAL_MAGIC = 0x5741


class BinaryWALWriter:
    """Append-only high-performance binary Write-Ahead Log for order events."""

    def __init__(self, wal_path: str):
        self.wal_path = wal_path
        self._seq = 0
        self._file = open(wal_path, "ab", buffering=65536)

    def append(self, raw_order: bytes) -> int:
        """Appends a 29-byte order frame with CRC32 checksum to disk."""
        self._seq += 1
        payload_len = len(raw_order)
        header = struct.pack(WAL_HEADER_FORMAT, WAL_MAGIC, self._seq, payload_len)
        checksum = zlib.crc32(raw_order) & 0xFFFFFFFF
        trailer = struct.pack("<I", checksum)

        self._file.write(header + raw_order + trailer)
        return self._seq

    def append_batch(self, orders: List[bytes]) -> int:
        """Flushes a batch of orders into the WAL."""
        count = 0
        for o in orders:
            self.append(o)
            count += 1
        self._file.flush()
        return count

    def sync(self):
        """Forces operating system flush to physical storage."""
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self):
        if not self._file.closed:
            self.sync()
            self._file.close()


class BinaryWALReader:
    """Sequential reader and replay engine for corrupted state recovery."""

    def __init__(self, wal_path: str):
        self.wal_path = wal_path

    def replay_records(self) -> Generator[Tuple[int, bytes], None, None]:
        """Yields validated (sequence_id, raw_order) pairs, verifying CRC32 checksums."""
        if not os.path.exists(self.wal_path):
            return

        with open(self.wal_path, "rb") as f:
            while True:
                hdr_bytes = f.read(WAL_HEADER_SIZE)
                if len(hdr_bytes) < WAL_HEADER_SIZE:
                    break

                magic, seq, payload_len = struct.unpack(WAL_HEADER_FORMAT, hdr_bytes)
                if magic != WAL_MAGIC:
                    raise CorruptedWALException(f"Invalid WAL magic byte: {hex(magic)} at seq {seq}")

                payload = f.read(payload_len)
                trailer = f.read(4)
                if len(trailer) < 4:
                    raise CorruptedWALException("Incomplete WAL frame encountered during read")

                expected_crc = struct.unpack("<I", trailer)[0]
                actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    raise CorruptedWALException(f"CRC32 mismatch on WAL record seq {seq}")

                yield seq, payload


class CorruptedWALException(Exception):
    pass
