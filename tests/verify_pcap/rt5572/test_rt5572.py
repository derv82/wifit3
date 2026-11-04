from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.rt5572 import SUPPORTED_IDS, constants as C
from wifit3.chips.rt5572.chan import _RF_VALS_5592_XTAL40
from wifit3.chips.rt5572.driver import RT5572Driver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rt2800usb_rt5572"
PAU09_CAPTURE_DIR = DRIVER_CAPTURES / "rt5572"
# rt2800_config_channel_rf55xx (rt2800lib.c:3503-3509): RFCSR8 = N & 0xff; RFCSR9_K = K, RFCSR9_N = N bit 8. Every
# capture's MAC_DEBUG_INDEX reads XTAL set: rf_vals_5592_xtal40.
CHANNEL_RFCSR_VALUES: dict[int, tuple[int, int]] = {}
for channel, (n, k, _mod, _r) in _RF_VALS_5592_XTAL40.items():
    CHANNEL_RFCSR_VALUES[channel] = (n & 0xff, (k & C.RFCSR9_K) | ((n & 0x100) >> 8 << 4))


class TestRt5572(VerifyCapture):
    CAPTURE_PATHS = [CAPTURE_DIR / "capture-1.pcap", CAPTURE_DIR / "capture-2.pcap",
                     CAPTURE_DIR / "capture-3.pcap",
                     DRIVER_CAPTURES / "captures_rt2800usb_rt5572_2" / "capture-1.pcap",
                     DRIVER_CAPTURES / "captures_rt2800usb" / "capture-1.pcap",
                     PAU09_CAPTURE_DIR / "pau09n600_1.pcap", PAU09_CAPTURE_DIR / "pau09n600_2.pcap",
                     PAU09_CAPTURE_DIR / "pau09n600_3.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RT5572Driver
    DECODER = Rt2800Decoder(channel_rf3_mask=C.RFCSR9_K | C.RFCSR9_N, channel_rfcsr_values=CHANNEL_RFCSR_VALUES,
                            txwi_size=C.TXWI_DESC_SIZE_5WORDS)
    WAIVERS = []
