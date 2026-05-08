import struct
from typing import Tuple, Optional

class HTCProtocol:
    """
    Handles Layer 1: Host-Target Communication (HTC).
    Responsible for transport headers and credit flow control.
    """
    
    # HTC Header: [Endpoint(1)] [Flags(1)] [PayloadLen(2, BE)] [Padding(2)]
    # Total 6 bytes.
    HTC_HDR_FMT = ">BBHH"
    HTC_HDR_LEN = 6

    def __init__(self):
        self.total_credits = 0
        self._credits_available = 0

    @property
    def credits(self) -> int:
        return self._credits_available

    def update_credits(self, count: int):
        """Updates the available TX credits from a firmware report."""
        self._credits_available = count

    def consume_credit(self):
        """Consumes a single credit for a TX operation."""
        if self._credits_available > 0:
            self._credits_available -= 1

    def pack(self, endpoint: int, payload: bytes, flags: int = 0) -> bytes:
        """
        Wraps a payload in an HTC header.
        """
        # Note: Some versions use a 4-byte header, but AR9271/ath9k_htc 
        # usually expects 6 bytes (4 header + 2 padding).
        header = struct.pack(self.HTC_HDR_FMT, endpoint, flags, len(payload), 0)
        return header + payload

    def unpack(self, data: bytes) -> Tuple[int, int, bytes]:
        """
        Unwraps an HTC packet.
        Returns: (endpoint, flags, payload)
        """
        if len(data) < self.HTC_HDR_LEN:
            raise ValueError(f"Packet too short for HTC header: {len(data)} bytes")

        endpoint, flags, payload_len, _ = struct.unpack(self.HTC_HDR_FMT, data[:self.HTC_HDR_LEN])
        
        # Guard against malformed length fields
        actual_payload = data[self.HTC_HDR_LEN : self.HTC_HDR_LEN + payload_len]
        return endpoint, flags, actual_payload

    def parse_credit_report(self, data: bytes) -> Optional[int]:
        """
        Checks if a packet is an HTC Credit Report and extracts the count.
        Typically found in packets starting with EP 1 or EP 0 depending on firmware.
        """
        # Based on research: EP 1, Flags 0, with credit count at offset 16 (for some FW)
        # or as part of a dedicated HTC Control message on EP 0.
        if len(data) >= 16 and data[0] == 0x01:
            # Simple heuristic from wmi_state.py
            return data[16]
        return None
