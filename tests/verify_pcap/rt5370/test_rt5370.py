from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rt5370 import SUPPORTED_IDS
from wifit3.chips.rt5370.driver import RT5370Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rt2800usb_rt5370"


class TestRt5370(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT5370Driver
    DECODER = Rt2800Decoder()
