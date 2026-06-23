"""`mt76x02_mac_setaddr` + `mt76x02_mac_set_bssid`.

Kernel reference: mt76x02_mac.c:727-758 (setaddr) and 1232-1238 (set_bssid).
"""
import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import mac


class FakeTransport:
    """Records writes + rmws; default reads return 0."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.rmws: list[tuple[int, int, int]] = []
        self.regs: dict[int, int] = {}

    def read32(self, addr: int) -> int:
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.regs[addr] = value
        self.writes.append((addr, value))

    def rmw32(self, addr: int, mask: int, value: int) -> None:
        cur = self.regs.get(addr, 0)
        new = ((cur & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.regs[addr] = new
        self.rmws.append((addr, mask, value))


SAMPLE_MAC = bytes.fromhex("0011223344FF")   # 00:11:22:33:44:FF


# ---------------------------------------------------------------------------
# mac_set_bssid — single-slot semantics
# ---------------------------------------------------------------------------

def test_set_bssid_writes_low_and_rmws_high():
    t = FakeTransport()
    mac.mac_set_bssid(t, idx=0, addr=SAMPLE_MAC)
    # Low DW: u32 LE of bytes 0..3
    expected_low = 0x33221100
    assert (C.MT_MAC_APC_BSSID_BASE, expected_low) in t.writes
    # High DW: RMW with mask=0xFFFF, value=u16 LE of bytes 4..5
    expected_high = 0xFF44
    matched = [
        rmw for rmw in t.rmws
        if rmw == (C.MT_MAC_APC_BSSID_BASE + 4,
                   C.MT_MAC_APC_BSSID_H_ADDR_MASK, expected_high)
    ]
    assert matched, f"expected RMW not found in {t.rmws}"


def test_set_bssid_idx_masked_to_7():
    """Kernel does `idx &= 7` — idx=8 should hit slot 0."""
    t = FakeTransport()
    mac.mac_set_bssid(t, idx=8, addr=SAMPLE_MAC)
    # Slot 0 → MT_MAC_APC_BSSID_BASE + 0 * 8 = 0x1090
    assert any(addr == C.MT_MAC_APC_BSSID_BASE for addr, _ in t.writes)


def test_set_bssid_slot_7_is_highest():
    t = FakeTransport()
    mac.mac_set_bssid(t, idx=7, addr=SAMPLE_MAC)
    assert any(
        addr == C.MT_MAC_APC_BSSID_BASE + 7 * 8 for addr, _ in t.writes
    )


def test_set_bssid_rejects_wrong_length():
    t = FakeTransport()
    with pytest.raises(ValueError):
        mac.mac_set_bssid(t, idx=0, addr=b"\x00" * 5)


# ---------------------------------------------------------------------------
# mac_setaddr — full kernel sequence
# ---------------------------------------------------------------------------

def test_mac_setaddr_writes_addr_dw0_low_4_bytes_le():
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    assert (C.MT_MAC_ADDR_DW0, 0x33221100) in t.writes


def test_mac_setaddr_writes_addr_dw1_with_u2me_mask():
    """ADDR_DW1 must have mac[4:6] in low 16 bits AND U2ME_MASK=0xff in
    bits 23:16 — kernel mt76x02_mac.c:742-744."""
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    expected_dw1 = 0xFF44 | C.MT_MAC_ADDR_DW1_U2ME_MASK
    assert (C.MT_MAC_ADDR_DW1, expected_dw1) in t.writes


def test_mac_setaddr_writes_bssid_dw0_same_as_mac_low():
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    assert (C.MT_MAC_BSSID_DW0, 0x33221100) in t.writes


def test_mac_setaddr_writes_bssid_dw1_with_mbss_mode_3_and_local_bit():
    """BSSID_DW1 = mac high 2 bytes | (MBSS_MODE=3 in bits 17:16) |
    MBSS_LOCAL_BIT (bit 21). [SRC] mt76x02_mac.c:748-751."""
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    expected = (
        0xFF44
        | (3 << 16)          # MBSS_MODE = 3
        | (1 << 21)          # MBSS_LOCAL_BIT
    )
    assert (C.MT_MAC_BSSID_DW1, expected) in t.writes


def test_mac_setaddr_rmws_mbeacon_n_to_7_on_bssid_dw1():
    """Kernel mt76x02_mac.c:753 — `mt76_rmw_field(BSSID_DW1, MBEACON_N, 7)`."""
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    expected_value = (7 << 18) & C.MT_MAC_BSSID_DW1_MBEACON_N_MASK
    assert any(
        addr == C.MT_MAC_BSSID_DW1
        and mask == C.MT_MAC_BSSID_DW1_MBEACON_N_MASK
        and value == expected_value
        for addr, mask, value in t.rmws
    )


def test_mac_setaddr_clears_all_8_apc_bssid_slots():
    """Kernel runs 16 iterations of `mac_set_bssid(i, null)`; each call
    writes APC_BSSID_L (low DW) and RMW's APC_BSSID_H. With `idx &= 7`,
    each of the 8 unique slots gets hit twice. Net: 16 low-DW writes
    (all zero) + 16 high-DW RMWs (all zero into the ADDR field)."""
    t = FakeTransport()
    mac.mac_setaddr(t, SAMPLE_MAC)
    # Count writes that fall in the APC_BSSID L slots.
    apc_lo_writes = [
        (addr, val) for addr, val in t.writes
        if addr in {C.MT_MAC_APC_BSSID_BASE + i * 8 for i in range(8)}
    ]
    assert len(apc_lo_writes) == 16
    assert all(val == 0 for _, val in apc_lo_writes)

    apc_hi_rmws = [
        (addr, mask, val) for addr, mask, val in t.rmws
        if addr in {C.MT_MAC_APC_BSSID_BASE + i * 8 + 4 for i in range(8)}
    ]
    assert len(apc_hi_rmws) == 16
    assert all(val == 0 for _, _, val in apc_hi_rmws)


def test_mac_setaddr_rejects_wrong_length():
    t = FakeTransport()
    with pytest.raises(ValueError):
        mac.mac_setaddr(t, b"\x00" * 5)
