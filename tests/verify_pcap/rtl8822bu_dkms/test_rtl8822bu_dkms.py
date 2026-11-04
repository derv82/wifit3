from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8822bu_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl88x2bu"


class TestRtl8822buDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8822buDkmsDriver
    DECODER = RealtekDecoder(hop_start_register=0x2860, tx_descriptor_size=48)
