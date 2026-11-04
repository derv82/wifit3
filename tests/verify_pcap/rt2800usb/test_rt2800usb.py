from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rt2800usb import SUPPORTED_IDS
from wifit3.chips.rt2800usb.chan import _RF_VALS_3X
from wifit3.chips.rt2800usb.driver import RT2800USBDriver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rt2800usb_rt3572"
AWUS051NH_CAPTURE_DIR = DRIVER_CAPTURES / "rt3572"
# rt2800_config_channel_rf3052 (rt2800lib.c:2641-2642): RFCSR2 = rf1, RFCSR3 = rf3, over rf_vals_3x.
CHANNEL_RFCSR_VALUES: dict[int, tuple[int, int]] = {}
for channel, (rf1, _rf2, rf3) in _RF_VALS_3X.items():
    CHANNEL_RFCSR_VALUES[channel] = (rf1, rf3)


class TestRt2800usbRt3572(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap",
                     AWUS051NH_CAPTURE_DIR / "awus051nh2_1.pcap", AWUS051NH_CAPTURE_DIR / "awus051nh2_2.pcap",
                     AWUS051NH_CAPTURE_DIR / "awus051nh2_3.pcap",
                     DRIVER_CAPTURES / "captures_rt3572_tx_diff" / "aireplay.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT2800USBDriver
    DECODER = Rt2800Decoder(channel_rf1_rfcsr=2, channel_rf3_rfcsr=3, channel_rfcsr_values=CHANNEL_RFCSR_VALUES)
    WAIVERS = []
