from __future__ import annotations

from pathlib import Path

from tests.verify_pcap.realtek_decoder import RealtekDecoder
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.rtl8821cu_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver

CAPTURE_DIR = Path(__file__).resolve().parents[3] / "driver_captures" / "captures_rtl8821cu"
READ = RegisterAccess.READ
WRITE = RegisterAccess.WRITE
# phydm_dc_cancellation; the 0x0c10 / 0x0c14 offsets it writes last differ per capture (not compared).
DC_CANCELLATION_RUN = (
    RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004007), RegisterOp(WRITE, 0x08fc, 0x00000000),
    RegisterOp(READ, 0x0fa0, None), RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004000),
    RegisterOp(READ, 0x08f8, None), RegisterOp(WRITE, 0x08f8, 0x400000c0), RegisterOp(READ, 0x0522, None),
    RegisterOp(READ, 0x0520, None), RegisterOp(WRITE, 0x0520, 0x00ff2f0f), RegisterOp(READ, 0x0838, None),
    RegisterOp(WRITE, 0x0838, 0x87766463), RegisterOp(READ, 0x0a04, None), RegisterOp(READ, 0x0808, None),
    RegisterOp(WRITE, 0x0808, 0x24028211), RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x00ff800c),
    RegisterOp(READ, 0x0c50, None), RegisterOp(WRITE, 0x0c50, 0x0000007e), RegisterOp(READ, 0x2bbc, None),
    RegisterOp(WRITE, 0x0c90, 0x0ef80000), RegisterOp(WRITE, 0x0c90, 0x03300003), RegisterOp(WRITE, 0x0c90, 0x03e00064),
    RegisterOp(WRITE, 0x0c90, 0x03f0afce), RegisterOp(READ, 0x2bbc, None), RegisterOp(WRITE, 0x0c90, 0x0ef00000),
    RegisterOp(READ, 0x2bb8, None), RegisterOp(WRITE, 0x0c90, 0x0ee01000), RegisterOp(WRITE, 0x0c90, 0x03300003),
    RegisterOp(WRITE, 0x0c90, 0x03e00064), RegisterOp(WRITE, 0x0c90, 0x03f0280d), RegisterOp(READ, 0x2bb8, None),
    RegisterOp(WRITE, 0x0c90, 0x0ee00000), RegisterOp(READ, 0x0c00, None), RegisterOp(WRITE, 0x0c00, 0x00000004),
    RegisterOp(READ, 0x0e00, None), RegisterOp(WRITE, 0x0e00, 0x00000004), RegisterOp(READ, 0x198c, None),
    RegisterOp(WRITE, 0x198c, 0x00004007), RegisterOp(WRITE, 0x08fc, 0x00000200), RegisterOp(READ, 0x08f8, None),
    RegisterOp(WRITE, 0x08f8, 0x400000c0), RegisterOp(READ, 0x0a78, None), RegisterOp(WRITE, 0x0a78, 0x00000000),
    RegisterOp(READ, 0x08b4, None), RegisterOp(WRITE, 0x08b4, 0x000fc0c0), RegisterOp(READ, 0x0fa0, None),
    RegisterOp(READ, 0x08b4, None), RegisterOp(WRITE, 0x08b4, 0x000fc080), RegisterOp(READ, 0x198c, None),
    RegisterOp(WRITE, 0x198c, 0x00004000), RegisterOp(READ, 0x08f8, None), RegisterOp(WRITE, 0x08f8, 0x400000c0),
    RegisterOp(READ, 0x0c00, None), RegisterOp(WRITE, 0x0c00, 0x00000007), RegisterOp(READ, 0x0e00, None),
    RegisterOp(WRITE, 0x0e00, 0x00000007), RegisterOp(READ, 0x2bbc, None), RegisterOp(WRITE, 0x0c90, 0x0ef80000),
    RegisterOp(WRITE, 0x0c90, 0x03300003), RegisterOp(WRITE, 0x0c90, 0x03e00064), RegisterOp(WRITE, 0x0c90, 0x03f1afce),
    RegisterOp(READ, 0x2bbc, None), RegisterOp(WRITE, 0x0c90, 0x0ef00000), RegisterOp(READ, 0x2bb8, None),
    RegisterOp(WRITE, 0x0c90, 0x0ee01000), RegisterOp(WRITE, 0x0c90, 0x03300003), RegisterOp(WRITE, 0x0c90, 0x03e00064),
    RegisterOp(WRITE, 0x0c90, 0x03f0281d), RegisterOp(READ, 0x2bb8, None), RegisterOp(WRITE, 0x0c90, 0x0ee00000),
    RegisterOp(READ, 0x0c50, None), RegisterOp(WRITE, 0x0c50, 0x00000020), RegisterOp(WRITE, 0x0522, 0x00000000),
    RegisterOp(READ, 0x0838, None), RegisterOp(WRITE, 0x0838, 0x87766461), RegisterOp(READ, 0x0808, None),
    RegisterOp(WRITE, 0x0808, 0x34028211), RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x80ff800c),
    RegisterOp(READ, 0x0a9c, None), RegisterOp(WRITE, 0x0a9c, 0x3f100000), RegisterOp(READ, 0x0c10, None),
    RegisterOp(WRITE, 0x0c10, 0x3c000100), RegisterOp(READ, 0x0c10, None), RegisterOp(WRITE, 0x0c10, None),
    RegisterOp(READ, 0x0c14, None), RegisterOp(WRITE, 0x0c14, 0x3d000000), RegisterOp(READ, 0x0c14, None),
    RegisterOp(WRITE, 0x0c14, None),
)


class TestRtl8821cuDkms(VerifyCapture):
    CAPTURE_PATHS = [
        CAPTURE_DIR / "capture-1.pcap",
        CAPTURE_DIR / "capture-2.pcap",
        CAPTURE_DIR / "capture-3.pcap",
        CAPTURE_DIR / "capture-4.pcap",
    ]
    SUPPORTED_IDS = SUPPORTED_IDS
    DRIVER_CLASS = Rtl8821cuDkmsDriver
    DECODER = RealtekDecoder(hop_start_register=0x2860, tx_descriptor_size=48)
    WAIVERS = [
        Waiver(WaiverReason.WIFIT3_ONLY, "phydm.c:3490 phydm_dc_cancellation; wifit3 skips it: its ck320 clock "
               "restart kills RX on cold boots", capture_only_run=DC_CANCELLATION_RUN),
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
