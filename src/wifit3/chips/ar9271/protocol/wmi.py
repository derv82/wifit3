import logging
import os
import struct
import zlib
from typing import Tuple, Optional, Dict, Any
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)


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


# RX-gate diagnostic (DEBUG only). Tallies every frame that clears the firmware
# magic by 802.11 direction + addr1 type and records which acceptance gate it
# cleared or died at, highlighting EAPOL. Kept as a lean ar9271 RX debug aid
# after it pinned the QoS-padding/FCS bug (2026-05-25); not generalised across
# drivers yet (see project gap-audit). Zero cost unless DEBUG logging is on.
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
            # Non-RX WMI events share the event-ID space with RX in this
            # firmware; the magic distinguishes them. (Confirmed 2026-05-25:
            # magic-fail payloads are tiny ACK/CTS control frames, not data.)
            return None

        # Firmware-flagged RX error (cleanroom's rs_status byte). Cheap reject
        # before the CRC32 — catches every frame the hardware itself marked as
        # corrupt. The 0x01 -> always-FCS-fail correlation is 100 % across the
        # 30-frame dump from 2026-05-22.
        if payload[6] != cls._RX_STATUS_OK:
            cls._gate_diag(payload, "drop:rs_status")
            return None

        declared_len = int.from_bytes(payload[4:6], "big")
        if declared_len != len(payload) - cls.HTC_RX_HEADER_LEN:
            cls._gate_diag(payload, "drop:declared_len")
            return None

        frame = payload[cls.HTC_RX_HEADER_LEN:]

        # Remove the hardware's MAC-header alignment padding BEFORE the FCS
        # check. ath9k pads the header so the payload is 4-byte aligned
        # (padsize = hdrlen & 3 → 2 bytes for a 26-B QoS header), but the
        # over-the-air FCS was computed without it. Skipping this dropped
        # every QoS frame — i.e. most downlink-unicast data AND all of the
        # 4-way handshake (M1-M4 are QoS) — at the FCS gate below. Mirrors the
        # kernel's ath9k_rx_skb_postprocess. [HW-confirmed 2026-05-25: all 9
        # captured EAPOL frames validate FCS only after this strip.]
        frame = cls._strip_alignment_padding(frame)

        # 802.11 FCS — last 4 B of the frame, LE-encoded CRC32 over the rest.
        # Catches the residual ~8 % that the rs_status check misses (real RF
        # noise / partial captures the firmware didn't flag).
        expected_fcs = int.from_bytes(frame[-4:], "little")
        if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != expected_fcs:
            cls._gate_diag(payload, "drop:fcs")
            return None

        cls._gate_diag(payload, "pass")
        rssi_snr = payload[8]
        rssi = cls.NOISE_FLOOR_DBM + rssi_snr if rssi_snr > 0 else cls.NOISE_FLOOR_DBM
        return WlanFrameParser.parse_80211_frame(frame, rssi)

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
        """DEBUG-only: tally a post-magic frame by direction + acceptance-gate
        outcome and highlight EAPOL. A frame's 802.11 header is intact whenever
        we classify it (valid FC + addresses) even if the body/tail failed FCS,
        so the EAPOL EtherType scan works regardless of `reason` — handy for
        spotting handshake frames that pass *or* get dropped at any gate."""
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
