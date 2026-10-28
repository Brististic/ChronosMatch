import os
import struct
from typing import Iterator, List

class CorruptedWALException(Exception):
    pass

class BinaryWALWriter:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._f = open(filepath, "a+b")

    def append_record(self, raw_bytes: bytes) -> int:
        if not raw_bytes:
            return 0
        length = len(raw_bytes)
        record = struct.pack("<I", length) + raw_bytes
        self._f.write(record)
        self._f.flush()
        return len(record)

    def append_batch(self, batch: List[bytes]) -> int:
        if not batch:
            return 0
        payload = bytearray()
        for raw_bytes in batch:
            payload.extend(struct.pack("<I", len(raw_bytes)))
            payload.extend(raw_bytes)
        self._f.write(payload)
        self._f.flush()
        return len(payload)

    def create_checkpoint(self, checkpoint_seq: int, snapshot_data: bytes = b"") -> str:
        """Day 19 Member 1: Write an atomic checkpoint record to mark safe truncation point."""
        checkpoint_path = f"{self.filepath}.checkpoint"
        with open(checkpoint_path, "wb") as cp_file:
            header = struct.pack("<4sQI", b"CKPT", checkpoint_seq, len(snapshot_data))
            cp_file.write(header)
            if snapshot_data:
                cp_file.write(snapshot_data)
        return checkpoint_path

    def truncate_before_sequence(self, min_retain_seq: int, schema_seq_offset: int = 1):
        """Day 19 Member 1: Compact/truncate historical log records before a given sequence."""
        self._f.flush()
        reader = BinaryWALReader(self.filepath)
        retained_records = []

        for raw_record in reader.replay_records():
            if len(raw_record) >= 9:
                seq = struct.unpack_from("<Q", raw_record, schema_seq_offset)[0]
                if seq >= min_retain_seq:
                    retained_records.append(raw_record)
            else:
                retained_records.append(raw_record)

        temp_path = f"{self.filepath}.tmp"
        with open(temp_path, "wb") as tmp_f:
            tmp_writer = BinaryWALWriter(temp_path)
            tmp_writer.append_batch(retained_records)
            tmp_writer.close()

        self.close()
        os.replace(temp_path, self.filepath)
        self._f = open(self.filepath, "a+b")

    def close(self):
        if self._f and not self._f.closed:
            self._f.flush()
            self._f.close()


class BinaryWALReader:
    def __init__(self, filepath: str):
        self.filepath = filepath

    def replay_records(self) -> Iterator[bytes]:
        if not os.path.exists(self.filepath):
            return

        with open(self.filepath, "rb") as f:
            while True:
                length_bytes = f.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) < 4:
                    raise CorruptedWALException("Incomplete record length header in WAL.")

                length = struct.unpack("<I", length_bytes)[0]
                payload = f.read(length)
                if len(payload) < length:
                    raise CorruptedWALException("Incomplete payload read in WAL.")

                yield payload
