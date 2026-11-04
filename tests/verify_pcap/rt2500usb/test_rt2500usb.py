from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2500usb_decoder import Rt2500usbDecoder
from tests.verify_pcap.verify_capture import ProbeReply, VerifyCapture
from wifit3.chips.rt2500usb import SUPPORTED_IDS
from wifit3.chips.rt2500usb import constants as C
from wifit3.chips.rt2500usb.driver import RT2500USBDriver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rt2500usb"
CAPTURE_DIR_2 = DRIVER_CAPTURES / "captures_rt2500usb_2"

BBP17_READ_MISSING = "op 1: driver lacks validate_eeprom's BBP17 read (rt2500usb.c:1376), reads MAC_CSR0"


class TestRt2500usb(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap",
                     CAPTURE_DIR_2 / "capture-1.pcap", CAPTURE_DIR_2 / "capture-2.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT2500USBDriver
    MAC80211_DEFAULT_CHANNEL = 1
    DECODER = Rt2500usbDecoder()
    XFAIL_REASON_BY_CAPTURE_PATH = {
        CAPTURE_DIR / "capture-1.pcap": "op 134: driver writes PHY_CSR9 = 0x3000 (RF3 TX power), capture has 0x2800",
        CAPTURE_DIR / "capture-2.pcap": BBP17_READ_MISSING,
        CAPTURE_DIR / "capture-3.pcap": BBP17_READ_MISSING,
        CAPTURE_DIR_2 / "capture-1.pcap": BBP17_READ_MISSING,
        CAPTURE_DIR_2 / "capture-2.pcap": BBP17_READ_MISSING,
    }
    # capture-1 starts at init_registers. The EEPROM and MAC_CSR0 replies are capture-2's; MAC_CSR1's is capture-1's
    # first read, before any write to it.
    PROBE_REPLIES_BY_CAPTURE_PATH = {
        CAPTURE_DIR / "capture-1.pcap": (
            ProbeReply(C.USB_EEPROM_READ, 0, 0, C.EEPROM_SIZE, CAPTURE_DIR / "capture-2.pcap"),
            ProbeReply(C.USB_MULTI_READ, 0, C.MAC_CSR0, 2, CAPTURE_DIR / "capture-2.pcap"),
            ProbeReply(C.USB_MULTI_READ, 0, C.MAC_CSR1, 2, CAPTURE_DIR / "capture-1.pcap"),
        ),
    }
