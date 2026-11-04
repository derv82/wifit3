from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8822cu import SUPPORTED_IDS
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl88x2cu"


class TestRtl8822cu(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RTL8822CUDriver
    DECODER = RealtekDecoder(hop_start_register=0x3C60, rf_sipi_address=None, rf_base_address=0x3C00,
                             tx_descriptor_size=48)
