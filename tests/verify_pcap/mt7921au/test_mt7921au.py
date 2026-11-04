from __future__ import annotations

import struct
from pathlib import Path

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.mt7921au import SUPPORTED_IDS, constants as C, mcu
from wifit3.chips.mt7921au.driver import MT7921AUDriver

CAPTURES_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
PAU0F_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7921u_pau0f-no-adapter-scatter"
AXML_CAPTURE_DIR = CAPTURES_DIR / "captures_mt7921u_axml-no-adapter-scatter"
INJECTION_5GHZ_CAPTURE_PATH = CAPTURES_DIR / "captures_mt7921u_5g-injection" / "capture-1.pcap"
AXML_KERNEL_6_19_CAPTURE_PATH = CAPTURES_DIR / "captures_mt7921au_warm" / "capture-1.pcap"
# mt76_connac2_mcu_rxd (mt76_connac_mcu.h:117-126): rxd[6], len, pkt_type_id, then eid and seq.
CONNAC2_RXD_EID_OFFSET = 28
CONNAC2_RXD_SEQ_OFFSET = 29
# MediatekConnacDecoder's legacy command address: pq_id << 16 | ext_cid << 8 | cid.
SET_RX_FILTER_ADDRESS = (C.MCU_PQ_ID << 16) | mcu.CE_CMD_SET_RX_FILTER
SET_CHAN_DOMAIN_ADDRESS = (C.MCU_PQ_ID << 16) | mcu.CE_CMD_SET_CHAN_DOMAIN
CONFIGURE_FILTER_SOURCE = ("main.c:661-684 mt7921_configure_filter: mac80211 re-applies the monitor filter on "
                           "each ndo_set_rx_mode a capture tool causes (iface.c:857, main.c:93), "
                           "MCU SET_RX_FILTER fif 0x80000060")
# mcu.c:1451-1470 mt7921_mcu_set_rxfilter(dev, 0x80000060, 0, 0): rsv, mode 1, rsv2, fif, bit_map, bit_op, pad.
MONITOR_RX_FILTER_PAYLOAD = struct.pack("<4xB3xIIB51x", 1, 0x80000060, 0, 0)
# MT_TXD3_SEQ: txwi[3] bits 16..27, after the 4-byte SDIO header.
TXD3_SEQ = TxOpField(dword=(C.SDIO_HDR_SIZE + 3 * 4) // 4, shift=16, width=12)
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the SDIO header and 64-byte TXD.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(C.SDIO_HDR_SIZE + C.MT_SDIO_TXD_SIZE + 22) // 4, shift=20, width=12)
TXD3_NO_ACK = TxOpField(dword=(C.SDIO_HDR_SIZE + 3 * 4) // 4, shift=0, width=1)
# mt76_connac_mcu_set_channel_domain: a 12-byte header, then 8-byte {hw_value, pad, flags} per enabled channel;
# ch40 and ch48 are entries 15 and 17, after ch1-14 and ch36.
IEEE80211_CHAN_NO_IR = 1 << 1
CH40_CH48_NO_IR_BITS = (IEEE80211_CHAN_NO_IR << 8 * (12 + 8 * 15 + 4)) | (IEEE80211_CHAN_NO_IR << 8 * (12 + 8 * 17 + 4))


class TestMt7921au(VerifyCapture):
    CAPTURE_PATHS = [PAU0F_CAPTURE_DIR / "capture-1.pcap", PAU0F_CAPTURE_DIR / "capture-2.pcap",
                     PAU0F_CAPTURE_DIR / "capture-3.pcap", AXML_CAPTURE_DIR / "capture-1.pcap",
                     AXML_CAPTURE_DIR / "capture-2.pcap", AXML_CAPTURE_DIR / "capture-3.pcap",
                     INJECTION_5GHZ_CAPTURE_PATH, AXML_KERNEL_6_19_CAPTURE_PATH]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = MT7921AUDriver
    # mt7921_change_chanctx (main.c:1397-1398) sends only the sniffer config per hop.
    DECODER = MediatekConnacDecoder(rx_event_eid_offset=CONNAC2_RXD_EID_OFFSET,
                                    rx_event_seq_offset=CONNAC2_RXD_SEQ_OFFSET,
                                    sniffer_enable_starts_channel_change=False,
                                    legacy_command_register_ops=True)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, CONFIGURE_FILTER_SOURCE,
               capture_only_run=(RegisterOp(RegisterAccess.WRITE, SET_RX_FILTER_ADDRESS,
                                            int.from_bytes(MONITOR_RX_FILTER_PAYLOAD, "little"),
                                            len(MONITOR_RX_FILTER_PAYLOAD)),),
               driver_occurrence_count=2),
        Waiver(WaiverReason.HOST_VALUE, "mt76_connac_mcu.c:80-153 mt76_connac_mcu_set_channel_domain sends each "
               "channel's cfg80211 flags; this host's cfg80211 beacon hints had cleared NO_IR on ch40 and ch48",
               value_only_register=SET_CHAN_DOMAIN_ADDRESS, value_only_bits=CH40_CH48_NO_IR_BITS,
               capture_paths=(INJECTION_5GHZ_CAPTURE_PATH,)),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: Driver.inject_frame _stamp_tx_seq; aireplay-ng's radiotap NOSEQ "
               "keeps its own", value_only_tx_op_field=FRAME_SEQUENCE_NUMBER),
        Waiver(WaiverReason.WIFIT3_ONLY, "mt76_connac_mac.c:478-480 mt76_connac2_mac_write_txwi_80211 copies the "
               "injected frame's seq_ctrl into MT_TXD3_SEQ; wifit3 only: _stamp_tx_seq", value_only_tx_op_field=TXD3_SEQ),
        Waiver(WaiverReason.CAPTURE_SETUP, "mt76_connac_mac.c:564-565 mt76_connac2_mac_write_txwi MT_TXD3_NO_ACK: "
               "aireplay-ng's radiotap TX flags set NOACK, so mac80211 sets IEEE80211_TX_CTL_NO_ACK",
               value_only_tx_op_field=TXD3_NO_ACK),
    ]
