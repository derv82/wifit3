from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8821au_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl8821au"


class TestRtl8821auDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8821auDkmsDriver
    DECODER = RealtekDecoder()
