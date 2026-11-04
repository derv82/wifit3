from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rtl8187_decoder import Rtl8187Decoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8187 import SUPPORTED_IDS
from wifit3.chips.rtl8187.driver import RTL8187Driver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
AWUS036H_DIR = DRIVER_CAPTURES / "rtl8187"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rtl8187"
CAPTURE_DIR_2 = DRIVER_CAPTURES / "captures_rtl8187_2"


class TestRtl8187(VerifyCapture):
    CAPTURE_PATHS = [AWUS036H_DIR / "awus036h_1.pcap", AWUS036H_DIR / "awus036h_2.pcap",
                     AWUS036H_DIR / "awus036h_3.pcap",
                     CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap", CAPTURE_DIR / "capture-3.pcap",
                     CAPTURE_DIR_2 / "capture-1.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RTL8187Driver
    MAC80211_DEFAULT_CHANNEL = 1
    DECODER = Rtl8187Decoder()
    XFAIL_REASON_BY_CAPTURE_PATH = {
        capture_path: "op 1267: capture has a second rfkill GPIO0 poll, driver starts init_hw"
        for capture_path in CAPTURE_PATHS
    }
