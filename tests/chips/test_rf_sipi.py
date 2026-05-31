"""Shared rtw88 SIPI RF reader: per-path PI-mode register selection.

`read_rf` must take the PI-mode select bit from REG_3WIRE_SWB for path B
(rtw88xxa.c:1248), not REG_3WIRE_SWA. Reading the wrong SW register picks the
wrong PI/SI read register → garbage read-back → a corrupt RF read-modify-write,
which leaves the PHY deaf on 2T2R parts (8812a) after a few channel hops.
"""
from wifit3.chips.rtw88_base.rf_sipi import (
    REG_3WIRE_SWA,
    REG_3WIRE_SWB,
    REG_PI_READ_B,
    REG_SI_READ_A,
    REG_SI_READ_B,
    RFREG_MASK,
    read_rf,
)

from tests.chips.rtl8821au.test_mac_init import MockTransport


def test_read_rf_path_b_uses_swb_for_pi_mode():
    """Path B reads PI-mode from SWB. SWA and SWB disagree here, and the PI vs
    SI source registers hold different values, so the wrong choice is visible."""
    t = MockTransport()
    t._store(REG_3WIRE_SWB, [0x04, 0, 0, 0])   # bit2=1 → PI mode (correct)
    t._store(REG_3WIRE_SWA, [0x00, 0, 0, 0])   # bit2=0 → SI mode (the bug)
    t._store(REG_PI_READ_B, [0x78, 0x56, 0, 0])  # correct source → 0x5678
    t._store(REG_SI_READ_B, [0xFF, 0xFF, 0, 0])  # what the bug would have read

    val = read_rf(t, addr=0x18, mask=RFREG_MASK, path="b")

    assert val == 0x5678
    assert ("r32", REG_3WIRE_SWB) in t.reads
    assert ("r32", REG_3WIRE_SWA) not in t.reads


def test_read_rf_path_a_still_uses_swa():
    """Path A is unchanged — 1T1R parts (8821a) only ever use this path."""
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0x00, 0, 0, 0])   # bit2=0 → SI mode
    t._store(REG_SI_READ_A, [0x34, 0x12, 0, 0])

    val = read_rf(t, addr=0x18, mask=RFREG_MASK, path="a")

    assert val == 0x1234
    assert ("r32", REG_3WIRE_SWA) in t.reads
    assert ("r32", REG_3WIRE_SWB) not in t.reads
