from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tests.verify_pcap.realtek_decoder import RealtekDecoder, is_register_access, register_value
from tests.verify_pcap.usb_capture import UsbOp
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rtl8822bu_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl88x2bu"
READ = RegisterAccess.READ
WRITE = RegisterAccess.WRITE
# phydm_rfe_8822b(2.4 GHz) from the 5 GHz pinmux (0x0cb0 reads 0x77777777): 0x0cb0/0x0eb0, 0x0cb4/0x0eb4,
# inverse 0x0cbc/0x0ebc, 0x0ca0/0x0ea0.
RFE_2G_PINMUX_RUN = (
    RegisterOp(READ, 0x0cb0, 0x77777777), RegisterOp(WRITE, 0x0cb0, 0x77745774),
    RegisterOp(READ, 0x0eb0, None), RegisterOp(WRITE, 0x0eb0, 0x77745774),
    RegisterOp(READ, 0x0cb4, None), RegisterOp(WRITE, 0x0cb4, 0x00005777),
    RegisterOp(READ, 0x0eb4, None), RegisterOp(WRITE, 0x0eb4, 0x00005777),
    RegisterOp(READ, 0x0cbc, None), RegisterOp(WRITE, 0x0cbc, 0x00000000),
    RegisterOp(READ, 0x0cbc, None), RegisterOp(WRITE, 0x0cbc, 0x00000000),
    RegisterOp(READ, 0x0ebc, None), RegisterOp(WRITE, 0x0ebc, 0x00000000),
    RegisterOp(READ, 0x0ebc, None), RegisterOp(WRITE, 0x0ebc, 0x00000000),
    RegisterOp(READ, 0x0ca0, None), RegisterOp(WRITE, 0x0ca0, 0x0000a501),
    RegisterOp(READ, 0x0ea0, None), RegisterOp(WRITE, 0x0ea0, 0x0000a501),
)


class Rtl8822buDecoder(RealtekDecoder):
    """config_phydm_switch_band_8822b writes RF 0x18 with the old channel and the new band bits, then
    config_phydm_switch_channel_8822b re-reads it to tune (rtl8822b_phy.c:830-845): one hop."""

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        channel = super().channel_change_at(ops, position)
        if channel is None or channel != register_value(ops[position]) & 0xFF:
            return channel
        for later in range(position + 1, len(ops)):
            op = ops[later]
            if is_register_access(op) and op.is_in and op.value == self.hop_start_register:
                return super().channel_change_at(ops, later)
        return channel


class TestRtl8822buDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8822buDkmsDriver
    DECODER = Rtl8822buDecoder(hop_start_register=0x2860, tx_descriptor_size=48)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, "phydm_hal_api8822b.c:2567 phydm_rfe_8822b 2.4 GHz pinmux: a no-op on a "
               "fresh module load (capture-1), written after the previous capture left the 5 GHz pinmux",
               capture_only_run=RFE_2G_PINMUX_RUN,
               capture_paths=(CAPTURE_DIR / "capture-2.pcap", CAPTURE_DIR / "capture-3.pcap")),
    ]
