from __future__ import annotations

import struct
from pathlib import Path

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.mt7925au import SUPPORTED_IDS, constants as C
from wifit3.chips.mt7925au.driver import MT7925AUDriver

CAPTURES_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
RX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u"
TX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u_tx"
CONFIGURE_FILTER_SOURCE = ("main.c:778-804 mt7925_configure_filter: mac80211 re-applies the monitor filter as "
                           "capture tools open their sockets, MCU BAND_CONFIG tag 12 fif 0x80000060")
# mcu.c:3791-3820 mt7925_mcu_set_rxfilter(dev, 0x80000060, 0, 0): band_idx, tag, len, mode, fif, bit_map, bit_op, pad.
MONITOR_RX_FILTER_PAYLOAD = struct.pack("<B3xHHB3xIIB51x", 0, 12, 68, 0, 0x80000060, 0, 0)


class TestMt7925au(VerifyCapture):
    CAPTURE_PATHS = [RX_CAPTURE_DIR / "capture-1.pcap", RX_CAPTURE_DIR / "capture-2.pcap",
                     RX_CAPTURE_DIR / "capture-3.pcap", TX_CAPTURE_DIR / "capture-1.pcap",
                     TX_CAPTURE_DIR / "capture-2.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = MT7925AUDriver
    DECODER = MediatekConnacDecoder()
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, CONFIGURE_FILTER_SOURCE,
               capture_only_run=(RegisterOp(RegisterAccess.WRITE, C.MCU_UNI_CMD_BAND_CONFIG,
                                            int.from_bytes(MONITOR_RX_FILTER_PAYLOAD, "little")),)),
    ]
