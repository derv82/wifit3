from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.verify_capture import VerifyCapture
from wifit3.chips.mt7921au import SUPPORTED_IDS
from wifit3.chips.mt7921au.driver import MT7921AUDriver

CAPTURES_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
PAU0F_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7921u_pau0f-no-adapter-scatter"
AXML_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7921u_axml-no-adapter-scatter"
INJECTION_5GHZ_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7921u_5g-injection"
# mt76_connac2_mcu_rxd (mt76_connac_mcu.h:117-126): rxd[6], len, pkt_type_id, then eid and seq.
CONNAC2_RXD_EID_OFFSET = 28
CONNAC2_RXD_SEQ_OFFSET = 29


class TestMt7921au(VerifyCapture):
    CAPTURE_PATHS = [PAU0F_CAPTURE_DIR / "capture-1.pcap", PAU0F_CAPTURE_DIR / "capture-2.pcap",
                     PAU0F_CAPTURE_DIR / "capture-3.pcap", AXML_CAPTURE_DIR / "capture-1.pcap",
                     AXML_CAPTURE_DIR / "capture-2.pcap", AXML_CAPTURE_DIR / "capture-3.pcap",
                     INJECTION_5GHZ_CAPTURE_DIR / "capture-1.pcap"]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = MT7921AUDriver
    # mt7921_change_chanctx (main.c:1397-1398) sends only the sniffer config per hop.
    DECODER = MediatekConnacDecoder(rx_event_eid_offset=CONNAC2_RXD_EID_OFFSET,
                                    rx_event_seq_offset=CONNAC2_RXD_SEQ_OFFSET,
                                    sniffer_enable_starts_channel_change=False)
    WAIVERS = []
