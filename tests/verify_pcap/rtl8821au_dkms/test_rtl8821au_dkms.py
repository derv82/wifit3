from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rtl8821au_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl8821au"
READ = RegisterAccess.READ
WRITE = RegisterAccess.WRITE
# hw_var_set_opmode(STATION): REG_MACID (the card's MAC, not compared), StopTxBeacon, BCN_CTRL.
STATION_OPMODE_RUN = (
    RegisterOp(WRITE, 0x0610, None), RegisterOp(WRITE, 0x0611, None), RegisterOp(WRITE, 0x0612, None),
    RegisterOp(WRITE, 0x0613, None), RegisterOp(WRITE, 0x0614, None), RegisterOp(WRITE, 0x0615, None),
    RegisterOp(READ, 0x0550, None), RegisterOp(READ, 0x0102, None), RegisterOp(WRITE, 0x0102, 0x02),
    RegisterOp(READ, 0x0422, None), RegisterOp(WRITE, 0x0422, 0x31), RegisterOp(WRITE, 0x0541, 0x64),
    RegisterOp(READ, 0x0542, None), RegisterOp(WRITE, 0x0542, 0x00), RegisterOp(WRITE, 0x0550, 0x19),
)


class TestRtl8821auDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8821auDkmsDriver
    DECODER = RealtekDecoder()
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, "rtl8812a_hal_init.c:3744+ hw_var_set_opmode(STATION), via "
               "rtw_hal_init_opmode hal_intf.c:623: the capture's netdev opens in station mode",
               capture_only_run=STATION_OPMODE_RUN),
    ]
