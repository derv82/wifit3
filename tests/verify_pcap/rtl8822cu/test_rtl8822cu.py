from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import HALMAC_TX_DESC_RTS_DATA_RTY_LMT, HALMAC_TX_DESC_SW_SEQ, RealtekDecoder
from tests.verify_pcap.verify_capture import TxOpField, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rtl8822cu import SUPPORTED_IDS
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl88x2cu"
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the 48-byte TX descriptor.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(48 + 22) // 4, shift=20, width=12)


class TestRtl8822cu(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        # No capture-3: the vendor driver failed it (CFG_PARA timeout, then -71; driver.log:5640-6037).
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = RTL8822CUDriver
    DECODER = RealtekDecoder(hop_start_register=0x3C60, rf_sipi_address=None, rf_base_address=0x3C00,
                             tx_descriptor_size=48, tx_descriptor_checksum_size=48)
    WAIVERS = [
        Waiver(WaiverReason.WIFIT3_ONLY, "rtl8822cu_xmit.c:119-120 SW_SEQ is the frame's; wifit3 _stamp_tx_seq",
               value_only_tx_op_field=HALMAC_TX_DESC_SW_SEQ),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 _stamp_tx_seq numbers from 1; aireplay from 0",
               value_only_tx_op_field=FRAME_SEQUENCE_NUMBER),
        Waiver(WaiverReason.CAPTURE_SETUP, "core/rtw_xmit.c:5019 aireplay's radiotap NOACK, rtl8822cu_xmit.c:122-128",
               value_only_tx_op_field=HALMAC_TX_DESC_RTS_DATA_RTY_LMT),
    ]
