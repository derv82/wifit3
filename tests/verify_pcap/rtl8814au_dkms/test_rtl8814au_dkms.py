from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8814au_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures"


class TestRtl8814auDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "captures_rtl8814au" / "capture-1.pcap",
        CAPTURE_DIR / "captures_rtl8814au" / "capture-2.pcap",
        CAPTURE_DIR / "captures_rtl8814au" / "capture-3.pcap",
        CAPTURE_DIR / "captures_rtl8814au_2" / "capture-1.pcap",
        CAPTURE_DIR / "captures_rtl8814au_2" / "capture-2.pcap",
        CAPTURE_DIR / "captures_rtl8814au_2" / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8814auDkmsDriver
    DECODER = RealtekDecoder()
