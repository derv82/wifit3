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


# wifit3's _prime_2g_band: connect re-runs chan.set_channel(1) after the antenna switch.
PRIME_2G_BAND_RUN = (
    RegisterOp(READ, 0x0430, None), RegisterOp(READ, 0x0434, None), RegisterOp(READ, 0x042a, None),
    RegisterOp(READ, 0x0455, None), RegisterOp(READ, 0x06c0, None), RegisterOp(READ, 0x06c4, None),
    RegisterOp(WRITE, 0x06c0, 0x66555555), RegisterOp(WRITE, 0x06c4, 0x5a5a5a5a), RegisterOp(WRITE, 0x06c8, 0xf0ffffff),
    RegisterOp(WRITE, 0x06cc, 0x0000001b), RegisterOp(READ, 0x0550, None), RegisterOp(WRITE, 0x0550, 0x0000001c),
    RegisterOp(READ, 0x01cc, None), RegisterOp(WRITE, 0x01f4, 0x00001011), RegisterOp(WRITE, 0x01d4, 0x03306160),
    RegisterOp(READ, 0x2860, None), RegisterOp(READ, 0x0808, None), RegisterOp(WRITE, 0x0808, 0x34028211),
    RegisterOp(READ, 0x0454, None), RegisterOp(WRITE, 0x0454, 0x01cc7005), RegisterOp(READ, 0x0a80, None),
    RegisterOp(WRITE, 0x0a80, 0x21807532), RegisterOp(READ, 0x0814, None), RegisterOp(WRITE, 0x0814, 0x020c3d10),
    RegisterOp(READ, 0x1080, None), RegisterOp(WRITE, 0x04e0, 0x00000000), RegisterOp(WRITE, 0x1080, 0x00010000),
    RegisterOp(WRITE, 0x04e0, 0x00000000), RegisterOp(READ, 0x0000, None), RegisterOp(WRITE, 0x04e0, 0x00000098),
    RegisterOp(WRITE, 0x0000, 0xdc1f8e98), RegisterOp(WRITE, 0x04e0, 0x00000098), RegisterOp(READ, 0x0cb8, None),
    RegisterOp(READ, 0x0a84, None), RegisterOp(WRITE, 0x0a84, 0x800e0000), RegisterOp(READ, 0x0a80, None),
    RegisterOp(WRITE, 0x0a80, 0x2180fc84), RegisterOp(WRITE, 0x0cb8, 0x00010000), RegisterOp(READ, 0x2b7c, None),
    RegisterOp(WRITE, 0x0c90, 0x0df08049), RegisterOp(READ, 0x2990, None), RegisterOp(WRITE, 0x0c90, 0x0640000f),
    RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004007), RegisterOp(WRITE, 0x08fc, 0x00000000),
    RegisterOp(READ, 0x0fa0, None), RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004000),
    RegisterOp(READ, 0x08f8, None), RegisterOp(WRITE, 0x08f8, 0x400000c0), RegisterOp(READ, 0x0522, None),
    RegisterOp(READ, 0x0520, None), RegisterOp(WRITE, 0x0520, 0x00ff2f0f), RegisterOp(READ, 0x0838, None),
    RegisterOp(WRITE, 0x0838, 0x87766463), RegisterOp(READ, 0x0a04, None), RegisterOp(READ, 0x0808, None),
    RegisterOp(WRITE, 0x0808, 0x24028211), RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x00ff800c),
    RegisterOp(WRITE, 0x0c90, 0x01803c01), RegisterOp(WRITE, 0x0522, 0x00000000), RegisterOp(READ, 0x0838, None),
    RegisterOp(WRITE, 0x0838, 0x87766461), RegisterOp(READ, 0x0808, None), RegisterOp(WRITE, 0x0808, 0x34028211),
    RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x00ff800c), RegisterOp(READ, 0x0c1c, None),
    RegisterOp(WRITE, 0x0c1c, 0x40040053), RegisterOp(READ, 0x2860, None), RegisterOp(READ, 0x0c1c, None),
    RegisterOp(WRITE, 0x0c1c, 0x40040053), RegisterOp(READ, 0x0860, None), RegisterOp(WRITE, 0x0860, 0x72d5c321),
    RegisterOp(WRITE, 0x0a24, 0x64b80c1c), RegisterOp(READ, 0x0a28, None), RegisterOp(WRITE, 0x0a28, 0x00008810),
    RegisterOp(WRITE, 0x0aac, 0x01235667), RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004007),
    RegisterOp(WRITE, 0x08fc, 0x00000000), RegisterOp(READ, 0x0fa0, None), RegisterOp(READ, 0x198c, None),
    RegisterOp(WRITE, 0x198c, 0x00004000), RegisterOp(READ, 0x08f8, None), RegisterOp(WRITE, 0x08f8, 0x400000c0),
    RegisterOp(READ, 0x0522, None), RegisterOp(READ, 0x0520, None), RegisterOp(WRITE, 0x0520, 0x00ff2f0f),
    RegisterOp(READ, 0x0838, None), RegisterOp(WRITE, 0x0838, 0x87766463), RegisterOp(READ, 0x0a04, None),
    RegisterOp(READ, 0x0808, None), RegisterOp(WRITE, 0x0808, 0x24028211), RegisterOp(READ, 0x0a04, None),
    RegisterOp(WRITE, 0x0a04, 0x00ff800c), RegisterOp(WRITE, 0x0c90, 0x01803c01), RegisterOp(WRITE, 0x0522, 0x00000000),
    RegisterOp(READ, 0x0838, None), RegisterOp(WRITE, 0x0838, 0x87766461), RegisterOp(READ, 0x0808, None),
    RegisterOp(WRITE, 0x0808, 0x34028211), RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x00ff800c),
    RegisterOp(READ, 0x2b78, None), RegisterOp(WRITE, 0x0c90, 0x0de00201), RegisterOp(READ, 0x2b78, None),
    RegisterOp(WRITE, 0x0c90, 0x0de00221), RegisterOp(READ, 0x2954, None), RegisterOp(WRITE, 0x0c90, 0x05502073),
    RegisterOp(READ, 0x2994, None), RegisterOp(WRITE, 0x0c90, 0x06502060), RegisterOp(READ, 0x2954, None),
    RegisterOp(WRITE, 0x0c90, 0x05502073), RegisterOp(READ, 0x2954, None), RegisterOp(WRITE, 0x0c90, 0x05502073),
    RegisterOp(READ, 0x2994, None), RegisterOp(WRITE, 0x0c90, 0x06502060), RegisterOp(READ, 0x2994, None),
    RegisterOp(WRITE, 0x0c90, 0x06502060), RegisterOp(WRITE, 0x0483, 0x000000a0), RegisterOp(READ, 0x0668, None),
    RegisterOp(WRITE, 0x0668, 0x0e301000), RegisterOp(READ, 0x0024, None), RegisterOp(WRITE, 0x04e0, 0x000000df),
    RegisterOp(WRITE, 0x0024, 0x5dce1ddf), RegisterOp(WRITE, 0x04e0, 0x000000df), RegisterOp(WRITE, 0x055c, 0x00000050),
    RegisterOp(WRITE, 0x0638, 0x00000050), RegisterOp(READ, 0x0454, None), RegisterOp(WRITE, 0x0454, 0x00000005),
    RegisterOp(READ, 0x2860, None), RegisterOp(READ, 0x08ac, None), RegisterOp(WRITE, 0x08ac, 0xf00f0000),
    RegisterOp(READ, 0x08c4, None), RegisterOp(WRITE, 0x08c4, 0x47c00000), RegisterOp(READ, 0x198c, None),
    RegisterOp(WRITE, 0x198c, 0x00004007), RegisterOp(WRITE, 0x08fc, 0x00000000), RegisterOp(READ, 0x0fa0, None),
    RegisterOp(READ, 0x198c, None), RegisterOp(WRITE, 0x198c, 0x00004000), RegisterOp(READ, 0x08f8, None),
    RegisterOp(WRITE, 0x08f8, 0x400000c0), RegisterOp(READ, 0x0522, None), RegisterOp(READ, 0x0520, None),
    RegisterOp(WRITE, 0x0520, 0x00ff2f0f), RegisterOp(READ, 0x0838, None), RegisterOp(WRITE, 0x0838, 0x87766463),
    RegisterOp(READ, 0x0a04, None), RegisterOp(READ, 0x0808, None), RegisterOp(WRITE, 0x0808, 0x24028211),
    RegisterOp(READ, 0x0a04, None), RegisterOp(WRITE, 0x0a04, 0x00ff800c), RegisterOp(WRITE, 0x0c90, 0x01803c01),
    RegisterOp(WRITE, 0x0522, 0x00000000), RegisterOp(READ, 0x0838, None), RegisterOp(WRITE, 0x0838, 0x87766461),
    RegisterOp(READ, 0x0808, None), RegisterOp(WRITE, 0x0808, 0x34028211), RegisterOp(READ, 0x0a04, None),
    RegisterOp(WRITE, 0x0a04, 0x00ff800c), RegisterOp(READ, 0x0948, None), RegisterOp(WRITE, 0x0948, 0xac000000),
    RegisterOp(READ, 0x094c, None), RegisterOp(WRITE, 0x094c, 0x20000083), RegisterOp(READ, 0x0c20, None),
    RegisterOp(WRITE, 0x0c20, 0xc00503a3), RegisterOp(READ, 0x08f0, None), RegisterOp(WRITE, 0x08f0, 0x00009999),
    RegisterOp(READ, 0x0840, None), RegisterOp(WRITE, 0x0840, 0x171740e0), RegisterOp(READ, 0x0840, None),
    RegisterOp(WRITE, 0x0840, 0x171740f0), RegisterOp(WRITE, 0x1d00, 0x2d2d2d2d), RegisterOp(WRITE, 0x1d04, 0x2a2a2a2a),
    RegisterOp(WRITE, 0x1d08, 0x2a2a2a2a), RegisterOp(WRITE, 0x1d0c, 0x28282828), RegisterOp(WRITE, 0x1d10, 0x28282828),
    RegisterOp(WRITE, 0x1d2c, 0x28282828), RegisterOp(WRITE, 0x1d30, 0x28282828), RegisterOp(WRITE, 0x1d34, 0x00002828),
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
        Waiver(WaiverReason.WIFIT3_ONLY, "wifit3 _prime_2g_band: the cold ch1 tune runs before the antenna switch, "
               "so the 2.4 GHz LO never locks", driver_only_run=PRIME_2G_BAND_RUN),
    ]
