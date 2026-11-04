from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tests.verify_pcap.realtek_decoder import RealtekDecoder, is_register_access
from tests.verify_pcap.usb_capture import UsbOp
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rtl8821cu_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl8821cu"
WRITE = RegisterAccess.WRITE
REG_WL_0X430 = 0x0430
REG_BT_HI_PRI_TXRX = 0x0770


def is_register_read(op: UsbOp, address: int) -> bool:
    return is_register_access(op) and op.is_in and op.value == address


class Rtl8821cuDecoder(RealtekDecoder):
    """A band-switch hop opens with rtw_btcoex_switchband_notify's run_coex (its first op the limited_tx 0x0430
    read), before config_phydm_switch_band_8821c's RF 0x18 read (rtl8821c_phy.c:706-725)."""

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        if not is_register_read(ops[position], REG_WL_0X430):
            return super().channel_change_at(ops, position)
        for later in range(position + 1, len(ops)):
            if is_register_read(ops[later], REG_BT_HI_PRI_TXRX):
                return None
            if is_register_read(ops[later], self.hop_start_register):
                return super().channel_change_at(ops, later)
        return None


class TestRtl8821cuDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
        CAPTURE_DIR / "capture-4.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8821cuDkmsDriver
    DECODER = Rtl8821cuDecoder(hop_start_register=0x2860, tx_descriptor_size=48)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, "halbtc8821c1ant.c:467 halbtc8821c1ant_write_scbd's static originalval "
               "keeps the previous capture's scoreboard bits (module not reloaded)",
               value_only_register=0x00aa,
               capture_paths=(CAPTURE_DIR / "capture-2.pcap", CAPTURE_DIR / "capture-3.pcap")),
        Waiver(WaiverReason.CAPTURE_SETUP, "halbtc8821c1ant.c:467 halbtc8821c1ant_write_scbd skips a write equal to "
               "its static preval: the previous capture's bits were already written; "
               "usb_ops_linux.c:191 its 0x04e0 write",
               driver_only_run=(RegisterOp(WRITE, 0x00aa, 0x8403), RegisterOp(WRITE, 0x04e0, 0x03)),
               capture_paths=(CAPTURE_DIR / "capture-2.pcap", CAPTURE_DIR / "capture-3.pcap",
                              CAPTURE_DIR / "capture-4.pcap")),
        Waiver(WaiverReason.CAPTURE_SETUP, "halbtc8821c1ant.c:467 halbtc8821c1ant_write_scbd skips a write equal to "
               "its static preval, kept from the previous capture (module not reloaded); usb_ops_linux.c:191 its "
               "0x04e0 write",
               driver_only_run=(RegisterOp(WRITE, 0x00aa, 0x8003), RegisterOp(WRITE, 0x04e0, 0x03)),
               capture_paths=(CAPTURE_DIR / "capture-4.pcap",)),
    ]
