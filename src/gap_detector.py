from typing import List, Tuple, Dict


class SequenceGapDetector:
    def __init__(self, expected_start_seq: int = 1):
        self.expected_seq = expected_start_seq
        self.out_of_order_buffer: Dict[int, bytes] = {}
        self.detected_gaps: List[Tuple[int, int]] = []

    def process_packet(self, seq_num: int, packet_bytes: bytes) -> List[bytes]:
        ready_packets: List[bytes] = []

        if seq_num < self.expected_seq:
            return ready_packets

        if seq_num > self.expected_seq:
            self.detected_gaps.append((self.expected_seq, seq_num - 1))
            self.out_of_order_buffer[seq_num] = packet_bytes
            return ready_packets

        ready_packets.append(packet_bytes)
        self.expected_seq += 1

        while self.expected_seq in self.out_of_order_buffer:
            buffered_pkt = self.out_of_order_buffer.pop(self.expected_seq)
            ready_packets.append(buffered_pkt)
            self.expected_seq += 1

        return ready_packets

    def get_gap_count(self) -> int:
        return len(self.detected_gaps)

    def reset(self, start_seq: int = 1):
        self.expected_seq = start_seq
        self.out_of_order_buffer.clear()
        self.detected_gaps.clear()
