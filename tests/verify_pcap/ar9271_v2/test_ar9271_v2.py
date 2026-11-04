from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.ath9k_htc_decoder import Ath9kHtcDecoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.ar9271_v2 import SUPPORTED_IDS, reg as R
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures"
SOURCE_DIR = "driver_sources/ath9k-source-v6.18.12"
# ath9k_htc_calcrxfilter's ath9k_hw_getrxfilter (hw.c:2867-2878), then ath9k_hw_setrxfilter (hw.c:2881-2902).
MONITOR_RX_FILTER_RUN = (RegisterOp(RegisterAccess.READ, R.AR_RX_FILTER, None),
                         RegisterOp(RegisterAccess.READ, R.AR_PHY_ERR, None),
                         RegisterOp(RegisterAccess.SET_BITS, R.AR_RXCFG, 0),
                         RegisterOp(RegisterAccess.CLEAR_BITS, R.AR_RXCFG, R.AR_RXCFG_ZLFDMA),
                         RegisterOp(RegisterAccess.WRITE, R.AR_RX_FILTER, 0x0000C03F),
                         RegisterOp(RegisterAccess.WRITE, R.AR_PHY_ERR, 0))
LED_RUN = (RegisterOp(RegisterAccess.SET_BITS, R.AR_GPIO_IN_OUT, None),
           RegisterOp(RegisterAccess.CLEAR_BITS, R.AR_GPIO_IN_OUT, 1 << R.ATH_LED_PIN_9271))


class TestAr9271V2(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-1.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-2.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_newddevice" / "capture-3.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-1.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-2.pcap",
        CAPTURE_DIR / "captures_ath9k_htc" / "capture-3.pcap",
        CAPTURE_DIR / "captures_ath9k_htc_2" / "capture-1.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_1.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_2.pcap",
        CAPTURE_DIR / "ar9271" / "awus036nha_3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = AR9271V2Driver
    DECODER = Ath9kHtcDecoder()
    WAIVERS = [
        Waiver(WaiverReason.HOST_VALUE, "driver_sources/ath9k-source-v6.18.12/hw.c:1946-1947, tsf + elapsed us",
               value_only_register=R.AR_TSF_L32),
        Waiver(WaiverReason.CAPTURE_SETUP, f"{SOURCE_DIR}/htc_drv_main.c:1257-1279 ath9k_htc_configure_filter: "
               "mac80211 re-applies the monitor filter as capture tools open their sockets",
               capture_only_run=MONITOR_RX_FILTER_RUN, driver_occurrence_count=4),
        Waiver(WaiverReason.CAPTURE_SETUP, f"{SOURCE_DIR}/htc_drv_gpio.c:226-246 ath9k_led_work: mac80211's "
               "throughput LED trigger blinks the LED", capture_only_run=LED_RUN,
               driver_occurrence_count=2),
    ]
