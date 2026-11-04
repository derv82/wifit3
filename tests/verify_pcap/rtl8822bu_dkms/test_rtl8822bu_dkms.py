from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
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


class TestRtl8822buDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8822buDkmsDriver
    DECODER = RealtekDecoder(hop_start_register=0x2860, tx_descriptor_size=48)
    WAIVERS = [
        Waiver(WaiverReason.CAPTURE_SETUP, "phydm_hal_api8822b.c:2567 phydm_rfe_8822b 2.4 GHz pinmux: a no-op on a "
               "fresh module load (capture-1), written after the previous capture left the 5 GHz pinmux",
               capture_only_run=RFE_2G_PINMUX_RUN,
               capture_paths=(CAPTURE_DIR / "capture-2.pcap", CAPTURE_DIR / "capture-3.pcap")),
    ]
