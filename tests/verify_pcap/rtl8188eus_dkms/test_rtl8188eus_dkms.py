from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rtl8188eus_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures"


class TestRtl8188eusDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "captures_8188eu" / "capture-1.pcap",
        CAPTURE_DIR / "captures_8188eu" / "capture-2.pcap",
        CAPTURE_DIR / "captures_8188eu" / "capture-3.pcap",
        CAPTURE_DIR / "captures_8188eu_2" / "capture-1.pcap",
        CAPTURE_DIR / "captures_8188eu_icut" / "capture-1.pcap",
        CAPTURE_DIR / "captures_8188eu_icut" / "capture-2.pcap",
        CAPTURE_DIR / "captures_8188eu_icut" / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8188eusDkmsDriver
    DECODER = RealtekDecoder(hop_start_register=0x0E08, rf_sipi_address=0x0840, tx_descriptor_size=32,
                             report_type_dword_and_mask=(3, 0x3 << 14))
