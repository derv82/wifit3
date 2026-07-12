import logging
import os
import struct
import zlib
from typing import Tuple, Optional, Dict, Any
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)


# WIFIT3_AR9271_DUMP_RX=<N>: append the first N RX payloads (hex) to
# ar9271_rx_dump.log for offline RX-header analysis.
_RX_DUMP_COUNT = int(os.environ.get("WIFIT3_AR9271_DUMP_RX", "0"))
_RX_DUMP_REMAINING = [_RX_DUMP_COUNT]
_RX_DUMP_PATH = "ar9271_rx_dump.log"


def _dump_rx_payload(payload: bytes) -> None:
    if _RX_DUMP_REMAINING[0] <= 0:
        return
    _RX_DUMP_REMAINING[0] -= 1
    with open(_RX_DUMP_PATH, "a") as f:
        f.write(f"len={len(payload)}: {payload.hex()}\n")


# RX-gate diagnostic (DEBUG only, zero cost otherwise): tally each post-magic
# frame by 802.11 direction + which acceptance gate it cleared or died at,
# highlighting EAPOL. See _gate_diag.
_GATE = {"counts": {}, "calls": 0}


def _gate_dir_category(frame: bytes) -> str:
    """Classify a (post-magic) 802.11 frame for the RX-gate tally."""
    if len(frame) < 16:
        return "short"
    fc0, fc1 = frame[0], frame[1]
    if (fc0 & 0x03) != 0:
        return "badver"
    ftype = (fc0 & 0x0C) >> 2
    to_ds = fc1 & 0x01
    from_ds = (fc1 >> 1) & 0x01
    mcast = (frame[4] & 0x01) == 1  # addr1 (RA) first octet odd => mcast/bcast
    if ftype == 0:
        return "mgmt"
    if ftype == 1:
        return "ctrl"
    if ftype == 2:
        if from_ds and not to_ds:
            return "DL-mcast" if mcast else "DL-UNICAST"
        if to_ds and not from_ds:
            return "UL"
        return "data-other"
    return "unknown"


class WMIProtocol:
    """
    Handles Layer 2: Wireless Module Interface (WMI).
    Manages sequence IDs, command wrapping, and event parsing.
    """
    
    # WMI Header: [CommandID(2, BE)] [SequenceID(2, BE)]
    # Note: These 4 bytes are usually preceded by padding in the HTC payload.
    WMI_HDR_FMT = ">HH"
    WMI_HDR_LEN = 4

    # htc_9271_cleanroom.fw RX header — 36 B (decode via WIFIT3_AR9271_DUMP_RX).
    # NOT the kernel ath_htc_rx_status struct (mainline FW uses 40 B).
    #   off 0-3    u32       frame counter
    #   off 4-5    be16      802.11 frame length (excluding this 36-B header)
    #   off 6      u8        rs_status — 0x00 = clean, non-zero = FW-flagged
    #                        RX error (corrupt / bad FCS)
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
            # Non-RX WMI events share the event-ID space with RX in this
            # firmware; the magic distinguishes them (magic-fail payloads are
            # tiny control frames, never data).
            return None

        # Firmware-flagged RX error (rs_status byte) — a cheap reject before the
        # CRC32 for frames the hardware already marked corrupt.
        if payload[6] != cls._RX_STATUS_OK:
            cls._gate_diag(payload, "drop:rs_status")
            return None

        declared_len = int.from_bytes(payload[4:6], "big")
        if declared_len != len(payload) - cls.HTC_RX_HEADER_LEN:
            cls._gate_diag(payload, "drop:declared_len")
            return None

        frame = payload[cls.HTC_RX_HEADER_LEN:]

        # Strip the hardware's MAC-header alignment padding before the FCS check
        # (see _strip_alignment_padding). Without it every QoS frame — which is
        # all downlink data and the entire 4-way handshake — fails FCS.
        frame = cls._strip_alignment_padding(frame)

        # 802.11 FCS: last 4 B = LE CRC32 over the rest. Catches real RF noise /
        # partial captures the rs_status flag missed.
        expected_fcs = int.from_bytes(frame[-4:], "little")
        if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != expected_fcs:
            cls._gate_diag(payload, "drop:fcs")
            return None

        cls._gate_diag(payload, "pass")
        rssi_snr = payload[8]
        rssi = cls.NOISE_FLOOR_DBM + rssi_snr if rssi_snr > 0 else cls.NOISE_FLOOR_DBM
        # FCS check above validates the trailing CRC; drop the 4 FCS bytes
        # now before the parser hands frames to length-sensitive consumers
        # (WEP ARP detect, ChopChop ICV, Fragmentation seed).
        return WlanFrameParser.parse_80211_frame(frame[:-4], rssi)

    @classmethod
    def ack_ra(cls, payload: bytes) -> Optional[bytes]:
        """RA of a valid link-layer ACK in this RX event, else None — the raw pre-parse tap for
        TX-ACK detection. An ACK is a 10-byte 0xD4 MPDU (14 on the wire, +FCS); parse_rx_frame
        hands it to the parser, which drops control frames, so the ACK is read straight off the
        wire here. Applies the same RX-header gates parse_rx_frame does (magic distinguishes RX
        events, rs_status/declared-length/FCS reject corrupt frames)."""
        if len(payload) < cls.HTC_RX_HEADER_LEN + 14:
            return None
        if payload[10:16] != cls._RX_MAGIC or payload[6] != cls._RX_STATUS_OK:
            return None
        frame = payload[cls.HTC_RX_HEADER_LEN:]
        if len(frame) != 14 or frame[0] != 0xD4:
            return None
        if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != int.from_bytes(frame[-4:], "little"):
            return None
        return frame[4:10]

    @staticmethod
    def _strip_alignment_padding(frame: bytes) -> bytes:
        """Strip ath9k's DMA alignment padding inserted *after* the MAC header.

        The hardware pads the header so the 802.11 payload is 4-byte aligned;
        `padsize = ieee80211_hdrlen(fc) & 3` (0 for a 24-B mgmt/non-QoS header,
        2 for a 26-B QoS header). The over-the-air FCS excludes the pad, so it
        must be removed before both the FCS check and the parser. Mirrors
        ath9k_rx_skb_postprocess (kernel recv.c).
        """
        if len(frame) < 24:
            return frame
        fc0, fc1 = frame[0], frame[1]
        hdrlen = 24
        if (fc1 & 0x03) == 0x03:                            # 4-address (WDS)
            hdrlen += 6
        if ((fc0 & 0x0C) >> 2) == 0x02 and (fc0 & 0x80):    # QoS data subtype
            hdrlen += 2
        if fc1 & 0x80:                                       # HT Control (Order)
            hdrlen += 4
        padsize = hdrlen & 0x03
        if padsize and len(frame) >= hdrlen + padsize:
            return frame[:hdrlen] + frame[hdrlen + padsize:]
        return frame

    @classmethod
    def _gate_diag(cls, payload: bytes, reason: str) -> None:
        """DEBUG-only: tally a post-magic frame by direction + gate outcome and
        flag EAPOL. The 802.11 header is intact even when the body/tail fails
        FCS, so the EAPOL scan works for dropped frames too."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        frame = payload[cls.HTC_RX_HEADER_LEN:]
        cat = _gate_dir_category(frame)
        _GATE["counts"][(cat, reason)] = _GATE["counts"].get((cat, reason), 0) + 1
        _GATE["calls"] += 1
        if cat in ("DL-UNICAST", "DL-mcast", "UL", "data-other") and \
                b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" in frame:
            logger.debug("[RXGATE] EAPOL %s %s len=%d", cat, reason, len(frame))
        if _GATE["calls"] % 300 == 0:
            logger.debug("[RXGATE] tally: %s", dict(_GATE["counts"]))

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
