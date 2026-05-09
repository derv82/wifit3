import struct
from typing import Tuple, Optional

class HTCProtocol:
    """
    Handles Layer 1: Host-Target Communication (HTC).
    Responsible for transport headers and credit flow control.
    """
    
    # 4-byte header + 2-byte alignment padding = 6 bytes total offset
    HTC_HDR_FMT = ">BBHH"
    HTC_HDR_LEN = 6

    def __init__(self):
        self.total_credits = 0
        self._credits_available = 0

    @property
    def credits(self) -> int:
        return self._credits_available

    def update_credits(self, count: int):
        """Updates the available TX credits."""
        self._credits_available = count

    def consume_credit(self):
        """Consumes a single credit for a TX operation."""
        if self._credits_available > 0:
            self._credits_available -= 1

    def pack(self, endpoint: int, payload: bytes, flags: int = 0) -> bytes:
        """Wraps a payload in a 6-byte HTC header (4 bytes hdr + 2 bytes pad)."""
        # Length field includes the internal 2-byte alignment padding
        header = struct.pack(self.HTC_HDR_FMT, endpoint, flags, len(payload) + 2, 0)
        return header + payload

    def unpack(self, data: bytes) -> Tuple[int, int, bytes]:
        """Unwraps an HTC packet (skipping 6-byte header)."""
        if len(data) < self.HTC_HDR_LEN:
            raise ValueError(f"Packet too short for HTC header: {len(data)} bytes")

        endpoint, flags, payload_len, _ = struct.unpack(self.HTC_HDR_FMT, data[:self.HTC_HDR_LEN])
        # Skip 6 bytes total. actual_payload excludes the 2 bytes of internal padding.
        actual_payload = data[self.HTC_HDR_LEN : 4 + payload_len]
        return endpoint, flags, actual_payload

    def parse_credit_report(self, data: bytes) -> Optional[int]:
        """
        Checks for credit reports in traffic.
        Heuristic: EP 1, Flags 0, count at offset 16 (includes 6-byte header).
        """
        if len(data) >= 17 and data[0] == 0x01:
            return data[16]
        return None
