"""rtl8188ftv driver bring-up unit tests — chip_cut + vendor detection.

Locks the REG_SYS_CFG CHIP_VERSION extraction used by `_cold_bring_up`
(driver.py) to the kernel's bits 12-15 field (regs.h:342). A wrong-width
mask here silently picks the wrong RF table (`radio_a` vs `radio_a_cut_b`),
so the derivation itself is pinned to the captured wire value.
"""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8188ftv.constants import (
    FC0_TYPE_DATA,
    REG_MACID,
    REG_SYS_CFG,
    SYS_CFG_CHIP_VERSION_MASK,
    SYS_CFG_TRP_VAUX_EN,
    SYS_CFG_VENDOR_EXT_MASK,
    TX_DESC_SZ_8188F,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
)
from wifit3.chips.rtl8188ftv.driver import RTL8188FTVDriver
from wifit3.chips.rtl8188ftv.tx import build_deauth

# The real REG_SYS_CFG captured at cold boot (dev 8, capture-1).
# Kernel derives chip_cut = u32_get_bits(sys_cfg, 0xf000) == 1 (cut B).
CAPTURED_SYS_CFG = 0x04441525


def test_chip_version_mask_is_bits_12to15():
    assert SYS_CFG_CHIP_VERSION_MASK == 0xF000


def test_chip_cut_derivation_matches_capture():
    cut = (CAPTURED_SYS_CFG & SYS_CFG_CHIP_VERSION_MASK) >> 12
    assert cut == 1
    assert cut == (CAPTURED_SYS_CFG >> 12) & 0xF


def test_vendor_ext_mask_and_trp_bit():
    vendor = CAPTURED_SYS_CFG & SYS_CFG_VENDOR_EXT_MASK
    assert vendor == 0x0004_0000
    assert not (CAPTURED_SYS_CFG & SYS_CFG_TRP_VAUX_EN)


def test_reg_sys_cfg_constant_matches_wire():
    assert REG_SYS_CFG == 0x00F0


class _Recorder:
    """Records REG_MACID byte writes for driver active-monitor tests."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []

    def write8(self, addr, val):
        self.writes.append((addr, val))

    def read8(self, addr):
        return 0


def _driver() -> RTL8188FTVDriver:
    d = RTL8188FTVDriver(MagicMock())
    d.transport = _Recorder()
    return d


async def test_enter_active_monitor_programs_macid_and_returns_mac():
    d = _driver()
    mac = bytes.fromhex("020000000001")
    assert await d.enter_active_monitor(mac) == mac
    assert [(a, v) for a, v in d.transport.writes] == [
        (REG_MACID + i, b) for i, b in enumerate(mac)
    ]


async def test_exit_active_monitor_restores_efuse_mac():
    d = _driver()
    d._mac_bytes = bytes.fromhex("aabbccddeeff")
    await d.enter_active_monitor(bytes.fromhex("020000000001"))
    d.transport.writes.clear()
    await d.exit_active_monitor()
    assert [(a, v) for a, v in d.transport.writes] == [
        (REG_MACID + i, b) for i, b in enumerate(d._mac_bytes)
    ]


async def test_exit_active_monitor_without_efuse_is_noop():
    d = _driver()
    d._mac_bytes = None
    await d.enter_active_monitor(bytes.fromhex("020000000001"))
    d.transport.writes.clear()
    await d.exit_active_monitor()
    assert d.transport.writes == []


class _TxDevice:
    """Bulk-OUT write recorder returning the full count (no short writes)."""

    def __init__(self):
        self.writes: list[tuple[int, bytes, int]] = []

    def write(self, ep: int, data, timeout_ms: int) -> int:
        self.writes.append((ep, bytes(data), timeout_ms))
        return len(data)


def _data_frame() -> bytes:
    """Minimal 24-byte-header DATA frame (type 0x08, no QoS)."""
    return struct.pack(
        "<BBH6s6s6sH",
        FC0_TYPE_DATA, 0x00, 0x013A,
        bytes.fromhex("aabbccddeeff"),    # addr1 (DA)
        bytes.fromhex("001122334455"),    # addr2 (SA — the AP we spoof)
        bytes.fromhex("001122334455"),    # addr3 (BSSID)
        0,                                # seq_ctrl (stamped per inject)
    ) + b"\x10\x20" * 3


async def test_inject_frame_routes_data_to_low_lane_and_mgmt_to_mgmt():
    d = RTL8188FTVDriver(_TxDevice())
    d._mgmt_bulk_out = 0x02
    d._data_bulk_out = 0x03

    data = _data_frame()
    mgmt = build_deauth(bytes.fromhex("001122334455"), bytes.fromhex("ffeeddccbbaa"))

    assert await d._inject_frame(data)
    assert await d._inject_frame(mgmt)

    assert len(d.dev.writes) == 2
    (ep_data, urb_data, _), (ep_mgmt, urb_mgmt, _) = d.dev.writes
    assert ep_data == 0x03                      # LOW lane: BE data queue
    assert ep_mgmt == 0x02                      # HIGH lane: MGMT queue
    qd = (struct.unpack_from("<I", urb_data, 4)[0] >> TXDESC_QUEUE_SHIFT) & 0xFF
    qm = (struct.unpack_from("<I", urb_mgmt, 4)[0] >> TXDESC_QUEUE_SHIFT) & 0xFF
    assert qd == TXDESC_QUEUE_BE
    assert qm == TXDESC_QUEUE_MGNT
    assert urb_data[TX_DESC_SZ_8188F:] == data
    assert urb_mgmt[TX_DESC_SZ_8188F:] == mgmt