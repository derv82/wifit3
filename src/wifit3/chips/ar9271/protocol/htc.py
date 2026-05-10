import struct
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

class HTCProtocol:
    """
    Handles Layer 1: Host-Target Communication (HTC).
    Supports dual-header formats: 
    1. Standard 8-byte header (EP 0x83 / Control)
    2. 6-byte header + 2-byte alignment padding (EP 0x82 / WMI)
    """
    
    # Format: [Endpoint(1)] [Flags(1)] [PayloadLen(2, BE)] [Control/Trailer(4)]
    HTC_HDR_STD_FMT = ">BBH4s"
    HTC_HDR_STD_LEN = 8

    # Format for WMI: [Endpoint(1)] [Flags(1)] [PayloadLen(2, BE)] [Reserved(2)]
    # Note: WMI payload then starts after an additional 2 bytes of alignment.
    HTC_HDR_WMI_FMT = ">BBHH"
    HTC_HDR_WMI_LEN = 6

    def __init__(self):
        self.credits = 0
        self.credit_size = 0

    def update_credits(self, credits: int, credit_size: int = 0):
        self.credits = credits
        if credit_size:
            self.credit_size = credit_size
        logger.debug(f"HTC Credits Updated: {self.credits} (Size: {self.credit_size})")

    def consume_credit(self):
        if self.credits > 0:
            self.credits -= 1
            return True
        return False

    def pack_wmi(self, endpoint: int, payload: bytes, flags: int = 0) -> bytes:
        """
        Wraps a payload for WMI (EP 0x82/0x04) with 12-byte total shift.
        Logic: 8-byte HTC header (with 4 bytes for padding/shift) + 4 bytes WMI header.
        Actually, we use 6-byte HTC + 2-byte pad + 4-byte WMI in our implementation.
        """
        # Host -> Device DMA requires WMI payload at offset 12.
        # [6-byte HTC] + [2-byte Pad] = 8 bytes.
        # Then the WMI header (4 bytes) starts at offset 8.
        # WMI payload starts at offset 12.
        header = struct.pack(self.HTC_HDR_WMI_FMT, endpoint, flags, len(payload) + 2, 0)
        return header + b'\x00\x00' + payload

    def pack_control(self, endpoint: int, payload: bytes, flags: int = 0) -> bytes:
        """Wraps a payload for Control (EP 0x83/0x00) with 8-byte header."""
        header = struct.pack(self.HTC_HDR_STD_FMT, endpoint, flags, len(payload), b'\x00\x00\x00\x00')
        return header + payload

    def unpack(self, data: bytes, endpoint_address: int) -> Tuple[int, int, int, bytes]:
        """
        Unwraps an HTC packet. 
        Returns: (endpoint_id, flags, trailer_len, payload)
        Note: payload STILL CONTAINS the trailer.
        """
        if len(data) < self.HTC_HDR_STD_LEN:
            raise ValueError(f"Packet too short for HTC header: {len(data)} bytes")
            
        ep, flags, p_len, ctrl = struct.unpack(self.HTC_HDR_STD_FMT, data[:self.HTC_HDR_STD_LEN])
        
        # trailer_len is in the first byte of the 4-byte control field (ctrl[0])
        trailer_len = ctrl[0]
        
        # Extract payload (includes WMI + Trailer)
        payload = data[self.HTC_HDR_STD_LEN : self.HTC_HDR_STD_LEN + p_len]
        return ep, flags, trailer_len, payload

    def parse_ready_msg(self, payload: bytes) -> Optional[Tuple[int, int]]:
        """
        Parses HTC_MSG_READY_ID (0x0001).
        Format: [MsgID(2)] [Credits(2)] [CreditSize(2)] [MaxEPs(1)] [Pad(1)]
        """
        if len(payload) >= 6 and payload[0:2] == b'\x00\x01':
            msg_id, credits, size = struct.unpack(">HHH", payload[:6])
            return credits, size
        return None
