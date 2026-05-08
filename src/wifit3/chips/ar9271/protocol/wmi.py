import struct
from typing import Tuple, Optional

class WMIProtocol:
    """
    Handles Layer 2: Wireless Module Interface (WMI).
    Manages sequence IDs, command wrapping, and event parsing.
    """
    
    # WMI Header: [CommandID(2, BE)] [SequenceID(2, BE)]
    # Note: These 4 bytes are usually preceded by padding in the HTC payload.
    WMI_HDR_FMT = ">HH"
    WMI_HDR_LEN = 4

    def __init__(self):
        self._next_seq_id = 1

    def _get_next_seq(self) -> int:
        seq = self._next_seq_id
        self._next_seq_id = (self._next_seq_id % 254) + 1
        return seq

    def pack_command(self, command_id: int, payload: bytes = b'') -> Tuple[bytes, int]:
        """
        Wraps a WMI command and returns the packed bytes and the sequence ID used.
        """
        seq = self._get_next_seq()
        header = struct.pack(self.WMI_HDR_FMT, command_id, seq)
        return header + payload, seq

    def unpack_event(self, data: bytes) -> Tuple[int, int, bytes]:
        """
        Unwraps a WMI event from the raw HTC payload.
        Returns: (event_id, seq_id, payload)
        """
        if len(data) < self.WMI_HDR_LEN:
            raise ValueError(f"Data too short for WMI header: {len(data)} bytes")

        event_id, seq_id = struct.unpack(self.WMI_HDR_FMT, data[:self.WMI_HDR_LEN])
        payload = data[self.WMI_HDR_LEN:]
        return event_id, seq_id, payload

    # Common Command IDs
    WMI_ECHO_CMDID = 0x0001
    WMI_SET_PMMODE_CMDID = 0x0002
    WMI_SET_RX_FILTER_CMDID = 0x0012
    WMI_REG_READ_CMDID = 0x0014
    WMI_REG_WRITE_CMDID = 0x0015
    WMI_REG_RMW_CMDID = 0x0020

    # Common Event IDs
    WMI_READY_EVENTID = 0x0001
    WMI_CONNECT_EVENTID = 0x0002
    WMI_REG_RSP_EVENTID = 0x0013
    WMI_HWR_MODE_RESP_EVENTID = 0x0014
