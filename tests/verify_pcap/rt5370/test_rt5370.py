from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rt5370 import SUPPORTED_IDS, constants as C
from wifit3.chips.rt5370.driver import RT5370Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rt2800usb_rt5370"


class TestRt5370(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT5370Driver
    DECODER = Rt2800Decoder()
    WAIVERS = [
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: warm check (mac.is_chip_warm)",
               driver_only_run=(RegisterOp(RegisterAccess.READ, C.PBF_SYS_CTRL, None),)),
        Waiver(WaiverReason.CAPTURE_SETUP, "rt2x00mac.c:400 rt2x00mac_configure_filter -> rt2800lib.c:1967 "
               "rt2800_config_filter: mac80211 re-applies the monitor filter as a capture tool opens its socket",
               capture_only_run=(RegisterOp(RegisterAccess.READ, C.RX_FILTER_CFG, 0x93),
                                 RegisterOp(RegisterAccess.WRITE, C.RX_FILTER_CFG, 0x93))),
        Waiver(WaiverReason.WIFIT3_ONLY, "rt2800usb.c:155-169 rt2800usb_tx_dma_done -> "
               "rt2800usb_tx_sta_fifo_timeout; wifit3 skips it: inject_frame reads no TX status",
               capture_only_run=(RegisterOp(RegisterAccess.READ, C.TX_STA_FIFO, None),)),
    ]
