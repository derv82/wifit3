from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.ath9k_htc_decoder import Ath9kHtcDecoder
from tests.verify_pcap.verify_capture import VerifyCapture, Waiver, WaiverReason
from wifit3.chips.ar9271_v2 import SUPPORTED_IDS, reg as R
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures"


class TestAr9271V2(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-1.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-2.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-3.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-1.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-2.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-3.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_2" / "capture-1.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_1.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_2.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = AR9271V2Driver
    DECODER = Ath9kHtcDecoder()
    WAIVERS = [
        Waiver(WaiverReason.HOST_VALUE, "driver_sources/ath9k-source-v6.18.12/hw.c:1946-1947, tsf + elapsed us",
               value_only_register=R.AR_TSF_L32),
    ]
