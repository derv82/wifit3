from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.rt5572 import SUPPORTED_IDS, constants as C
from wifit3.chips.rt5572.chan import _RF_VALS_5592_XTAL40
from wifit3.chips.rt5572.driver import RT5572Driver

DRIVER_CAPTURES = Path(__file__).resolve().parents[3] / "driver_captures"
CAPTURE_DIR = DRIVER_CAPTURES / "captures_rt2800usb_rt5572"
PAU09_CAPTURE_DIR = DRIVER_CAPTURES / "rt5572"
# rt2800_config_channel_rf55xx (rt2800lib.c:3503-3509): RFCSR8 = N & 0xff; RFCSR9_K = K, RFCSR9_N = N bit 8. Every
# capture's MAC_DEBUG_INDEX reads XTAL set: rf_vals_5592_xtal40.
# The 802.11 sequence number: bits 4..15 of the frame's Sequence Control, after the 4-byte TXINFO and 20-byte TXWI.
FRAME_SEQUENCE_NUMBER = TxOpField(dword=(C.TXINFO_DESC_SIZE + C.TXWI_DESC_SIZE_5WORDS + 22) // 4, shift=20, width=12)
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
    MAC80211_DEFAULT_CHANNEL = 1
    # rt2800usb.c:94-169: txstatus_timer
    HRTIMER_TASKS = (RT5572Driver._tx_sta_fifo_timeout,)
    DECODER = Rt2800Decoder(channel_rf3_mask=C.RFCSR9_K | C.RFCSR9_N, channel_rfcsr_values=CHANNEL_RFCSR_VALUES,
                            txwi_size=C.TXWI_DESC_SIZE_5WORDS)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, "rt2x00mac.c:400 rt2x00mac_configure_filter -> rt2800lib.c:1967 "
               "rt2800_config_filter: mac80211 re-applies the monitor filter as a capture tool opens its socket",
               capture_only_run=(RegisterOp(RegisterAccess.READ, C.RX_FILTER_CFG, 0x93),
                                 RegisterOp(RegisterAccess.WRITE, C.RX_FILTER_CFG, 0x93))),
        Waiver(WaiverReason.CAPTURE_SETUP, "rt2x00queue.c:406-407 rt2x00queue_create_tx_descriptor -> rt2800lib.c:828 "
               "TXWI_W1_ACK: aireplay-ng's radiotap TX flags set NOACK, so mac80211 sets IEEE80211_TX_CTL_NO_ACK",
               value_only_tx_op_field=TxOpField(dword=2, shift=0, width=1)),
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 only: Driver.inject_frame _stamp_tx_seq; aireplay-ng's radiotap NOSEQ "
               "keeps its own", value_only_tx_op_field=FRAME_SEQUENCE_NUMBER),
        Waiver(WaiverReason.WIFIT3_ONLY, "rt2x00dev.c:88-113 rt2x00lib_disable_radio -> rt2800usb.c:352-358 "
               "STATE_RADIO_OFF: the capture ends with the interface brought down; close() makes these ops, but the "
               "replay calls close() only after the capture's last op",
               capture_only_run=(RegisterOp(RegisterAccess.READ, C.MAC_SYS_CTRL, None),
                                 RegisterOp(RegisterAccess.WRITE, C.MAC_SYS_CTRL, None),
                                 RegisterOp(RegisterAccess.READ, C.WPDMA_GLO_CFG, None),
                                 RegisterOp(RegisterAccess.WRITE, C.WPDMA_GLO_CFG, None),
                                 RegisterOp(RegisterAccess.READ, C.WPDMA_GLO_CFG, None),
                                 RegisterOp(RegisterAccess.READ, C.MAC_SYS_CTRL, None),
                                 RegisterOp(RegisterAccess.WRITE, C.MAC_SYS_CTRL, None),
                                 RegisterOp(RegisterAccess.READ, C.H2M_MAILBOX_CSR, None),
                                 RegisterOp(RegisterAccess.WRITE, C.H2M_MAILBOX_CSR, None),
                                 RegisterOp(RegisterAccess.WRITE, C.HOST_CMD_CSR, C.MCU_SLEEP),
                                 RegisterOp(RegisterAccess.READ, C.H2M_MAILBOX_CSR, None),
                                 RegisterOp(RegisterAccess.WRITE, C.H2M_MAILBOX_CSR, None),
                                 RegisterOp(RegisterAccess.WRITE, C.HOST_CMD_CSR, C.MCU_LED)),
               capture_paths=(PAU09_CAPTURE_DIR / "pau09n600_1.pcap", PAU09_CAPTURE_DIR / "pau09n600_2.pcap",
                              PAU09_CAPTURE_DIR / "pau09n600_3.pcap")),
    ]
