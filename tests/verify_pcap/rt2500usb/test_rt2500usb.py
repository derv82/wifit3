from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2500usb_decoder import Rt2500usbDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rt2500usb import SUPPORTED_IDS
from wifit3.chips.rt2500usb.driver import RT2500USBDriver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rt2500usb"
CAPTURE_DIR_2 = DRIVER_CAPTURES / "captures_rt2500usb_2"


class TestRt2500usb(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap",
                     CAPTURE_DIR_2 / "capture-1.pcap", CAPTURE_DIR_2 / "capture-2.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT2500USBDriver
    MAC80211_DEFAULT_CHANNEL = 1
    DECODER = Rt2500usbDecoder()
    XFAIL_REASON_BY_CAPTURE_PATH = {
        CAPTURE_DIR / "capture-1.pcap": "op 0: capture starts at init_registers, without the probe's EEPROM read",
        CAPTURE_DIR / "capture-2.pcap": "op 1: driver reads MAC_CSR0, capture has the BBP17 read (PHY_CSR8)",
        CAPTURE_DIR / "capture-3.pcap": "op 1: driver reads MAC_CSR0, capture has the BBP17 read (PHY_CSR8)",
        CAPTURE_DIR_2 / "capture-1.pcap": "op 1: driver reads MAC_CSR0, capture has the BBP17 read (PHY_CSR8)",
        CAPTURE_DIR_2 / "capture-2.pcap": "op 1: driver reads MAC_CSR0, capture has the BBP17 read (PHY_CSR8)",
    }
