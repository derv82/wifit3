from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.rtl8192fu import SUPPORTED_IDS, constants as C
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl8xxxu_8192fu"
MONITOR_RCR_WRITE = (RegisterOp(RegisterAccess.WRITE, C.REG_RCR, 0x7000702F),)
CONFIGURE_FILTER_SOURCE = ("core.c:6853 rtl8xxxu_configure_filter: mac80211 calls it repeatedly (10 RCR writes "
                           "per capture); the driver writes RCR once")
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the 40-byte TX descriptor.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(40 + 22) // 4, shift=20, width=12)


class TestRtl8192fu(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RTL8192FUDriver
    MAC80211_DEFAULT_CHANNEL = 1
    DECODER = RealtekDecoder(hop_start_register=0x0E08, rf_sipi_address=0x0840)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, CONFIGURE_FILTER_SOURCE, capture_only_run=MONITOR_RCR_WRITE,
               driver_occurrence_count=1),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: Driver.inject_frame _stamp_tx_seq; mac80211 leaves a monitor "
               "frame's own seq (tx.c:832-833)", value_only_tx_op_field=FRAME_SEQUENCE_NUMBER),
    ]
