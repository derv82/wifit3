from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.rt5370 import SUPPORTED_IDS, constants as C
from wifit3.chips.rt5370.driver import RT5370Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rt2800usb_rt5370"
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the 4-byte TXINFO and 16-byte TXWI.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(C.TXINFO_DESC_SIZE + C.TXWI_DESC_SIZE_4WORDS + 22) // 4, shift=20, width=12)


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
        Waiver(WaiverReason.CAPTURE_SETUP, "rt2x00queue.c:406-407 rt2x00queue_create_tx_descriptor -> rt2800lib.c:828 "
               "TXWI_W1_ACK: aireplay-ng's radiotap TX flags set NOACK, so mac80211 sets IEEE80211_TX_CTL_NO_ACK",
               value_only_tx_op_field=TxOpField(dword=2, shift=0, width=1)),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: Driver.inject_frame _stamp_tx_seq; aireplay-ng's radiotap NOSEQ "
               "keeps its own", value_only_tx_op_field=FRAME_SEQUENCE_NUMBER),
    ]
