from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rt3070 import SUPPORTED_IDS, constants as C
from wifit3.chips.rt3070.driver import RT3070Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rt3070"


class TestRt3070(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT3070Driver
    DECODER = Rt2800Decoder()
    WAIVERS = [
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: warm check (mac.is_chip_warm)",
               driver_only_run=(RegisterOp(RegisterAccess.READ, C.PBF_SYS_CTRL, None),)),
    ]
