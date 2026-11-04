from __future__ import annotations

import struct
from pathlib import Path

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.mt7925au import SUPPORTED_IDS, constants as C
from wifit3.chips.mt7925au.driver import MT7925AUDriver

CAPTURES_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
RX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u"
TX_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7925u_tx"
CONFIGURE_FILTER_SOURCE = ("main.c:786-812 mt7925_configure_filter: mac80211 re-applies the monitor filter on "
                           "each ndo_set_rx_mode a capture tool causes (iface.c:857, main.c:93), "
                           "MCU BAND_CONFIG tag 12 fif 0x80000060")
# mcu.c:3845-3874 mt7925_mcu_set_rxfilter(dev, 0x80000060, 0, 0): band_idx, tag, len, mode, fif, bit_map, bit_op, pad.
MONITOR_RX_FILTER_PAYLOAD = struct.pack("<B3xHHB3xIIB51x", 0, 12, 68, 0, 0x80000060, 0, 0)
# MT_TXD3_SEQ: txwi[3] bits 16..27, after the 4-byte SDIO header.
TXD3_SEQ = TxOpField(dword=(C.SDIO_HDR_SIZE + 3 * 4) // 4, shift=16, width=12)
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the SDIO header and 64-byte TXD.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(C.SDIO_HDR_SIZE + C.MT_SDIO_TXD_SIZE + 22) // 4, shift=20, width=12)
TXD3_NO_ACK = TxOpField(dword=(C.SDIO_HDR_SIZE + 3 * 4) // 4, shift=0, width=1)
TX_CAPTURE_PATHS = (TX_CAPTURE_DIR / "capture-1.pcap", TX_CAPTURE_DIR / "capture-2.pcap")


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
                                            int.from_bytes(MONITOR_RX_FILTER_PAYLOAD, "little"),
                                            len(MONITOR_RX_FILTER_PAYLOAD)),),
               driver_occurrence_count=2),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: Driver.inject_frame _stamp_tx_seq; aireplay-ng's radiotap NOSEQ "
               "keeps its own", value_only_tx_op_field=FRAME_SEQUENCE_NUMBER, capture_paths=TX_CAPTURE_PATHS),
        Waiver(WaiverReason.WIFIT3_ONLY, "mac.c:705-718 mt7925_mac_write_txwi_80211 copies the injected frame's "
               "seq_ctrl into MT_TXD3_SEQ; wifit3 only: _stamp_tx_seq", value_only_tx_op_field=TXD3_SEQ,
               capture_paths=TX_CAPTURE_PATHS),
        Waiver(WaiverReason.CAPTURE_SETUP, "mac.c:789-790 mt7925_mac_write_txwi MT_TXD3_NO_ACK: aireplay-ng's radiotap "
               "TX flags set NOACK, so mac80211 sets IEEE80211_TX_CTL_NO_ACK", value_only_tx_op_field=TXD3_NO_ACK,
               capture_paths=TX_CAPTURE_PATHS),
    ]
