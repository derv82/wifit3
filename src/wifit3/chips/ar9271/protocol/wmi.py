import os
import struct
import zlib
from typing import Tuple, Optional, Dict, Any
from wifit3.wlan.packet import WlanFrameParser


# Diagnostic: when WIFIT3_AR9271_DUMP_RX=<N> is set, the first N RX payloads
# are appended to ar9271_rx_dump.log in hex so we can decode the cleanroom-FW
# RX header layout offline. Each line is `len=<L>: <hex>`.
_RX_DUMP_COUNT = int(os.environ.get("WIFIT3_AR9271_DUMP_RX", "0"))
_RX_DUMP_REMAINING = [_RX_DUMP_COUNT]
_RX_DUMP_PATH = "ar9271_rx_dump.log"


def _dump_rx_payload(payload: bytes) -> None:
    if _RX_DUMP_REMAINING[0] <= 0:
        return
    _RX_DUMP_REMAINING[0] -= 1
    with open(_RX_DUMP_PATH, "a") as f:
        f.write(f"len={len(payload)}: {payload.hex()}\n")


class WMIProtocol:
    """
    Handles Layer 2: Wireless Module Interface (WMI).
    Manages sequence IDs, command wrapping, and event parsing.
    """
    
    # WMI Header: [CommandID(2, BE)] [SequenceID(2, BE)]
    # Note: These 4 bytes are usually preceded by padding in the HTC payload.
    WMI_HDR_FMT = ">HH"
    WMI_HDR_LEN = 4

    # htc_9271_cleanroom.fw RX header — 36 B, decoded 2026-05-22 from live
    # captures (see WIFIT3_AR9271_DUMP_RX). This is NOT the kernel
    # ath_htc_rx_status struct (mainline FW uses 40 B; cleanroom diverges).
    #   off 0-3    u32       frame counter
    #   off 4-5    be16      802.11 frame length (excluding this 36-B header)
    #   off 6      u8        rs_status — 0x00 = clean, 0x01 = bad-FCS / RX
    #                        error (cleanroom equivalent of mainline
    #                        rs_status & ATH9K_RXERR_*; verified empirically
    #                        2026-05-22, 100 % of byte6==1 frames fail FCS)
    #   off 7      u8        zero
    #   off 8      i8        RSSI chain-0 (dB above noise floor)
    #   off 9      i8        RSSI chain-1 (same as chain-0 on 1T1R AR9271)
    #   off 10-15  6 B       firmware magic: 80 16 80 80 01 FF
    #   off 16     u8        rate index
    #   off 17-23  zeros
    #   off 24     i8        RSSI duplicate
    #   off 25-35  chain/rate padding
    #   off 36..   802.11 frame (FCS in last 4 B, LE-encoded CRC32)
    HTC_RX_HEADER_LEN = 36
    NOISE_FLOOR_DBM = -95
    _RX_MAGIC = b"\x80\x16\x80\x80\x01\xff"
    _RX_STATUS_OK = 0x00

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

    @classmethod
    def parse_rx_frame(cls, payload: bytes) -> Optional[Dict[str, Any]]:
        """
        Parses a cleanroom-firmware RX event into a 802.11 frame dict.

        Drops the buffer when:
          * It's shorter than the 36-B header plus the 14-B minimum ACK frame.
          * The firmware magic at bytes 10-15 is missing (filters non-RX WMI
            events that share event-ID space with RX in this firmware).
          * The declared frame length doesn't match the on-wire length.
        """
        _dump_rx_payload(payload)

        if len(payload) < cls.HTC_RX_HEADER_LEN + 14:
            return None
        if payload[10:16] != cls._RX_MAGIC:
            return None

        # Firmware-flagged RX error (cleanroom's rs_status byte). Cheap reject
        # before the CRC32 — catches every frame the hardware itself marked as
        # corrupt. The 0x01 -> always-FCS-fail correlation is 100 % across the
        # 30-frame dump from 2026-05-22.
        if payload[6] != cls._RX_STATUS_OK:
            return None

        declared_len = int.from_bytes(payload[4:6], "big")
        if declared_len != len(payload) - cls.HTC_RX_HEADER_LEN:
            return None

        frame = payload[cls.HTC_RX_HEADER_LEN:]

        # 802.11 FCS — last 4 B of the frame, LE-encoded CRC32 over the rest.
        # Catches the residual ~8 % that the rs_status check misses (real RF
        # noise / partial captures the firmware didn't flag).
        expected_fcs = int.from_bytes(frame[-4:], "little")
        if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != expected_fcs:
            return None

        rssi_snr = payload[8]
        rssi = cls.NOISE_FLOOR_DBM + rssi_snr if rssi_snr > 0 else cls.NOISE_FLOOR_DBM
        return WlanFrameParser.parse_80211_frame(frame, rssi)

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
