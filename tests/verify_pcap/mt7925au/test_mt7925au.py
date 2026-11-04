from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.mt7925au import SUPPORTED_IDS
from wifit3.chips.mt7925au.driver import MT7925AUDriver

CAPTURES_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
RX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u"
TX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u_tx"


class TestMt7925au(VerifyCapture):
    CAPTURE_PATHS = [RX_CAPTURE_DIR / "capture-1.pcap", RX_CAPTURE_DIR / "capture-2.pcap",
                     RX_CAPTURE_DIR / "capture-3.pcap", TX_CAPTURE_DIR / "capture-1.pcap",
                     TX_CAPTURE_DIR / "capture-2.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = MT7925AUDriver
    DECODER = MediatekConnacDecoder()
