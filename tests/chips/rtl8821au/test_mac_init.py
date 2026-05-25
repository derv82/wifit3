"""Tests for the post-FW MAC init helpers.

Uses a recording mock transport so we can assert the exact register-write
sequence without touching real hardware. Verifies values for:

  * init_queue_reserved_page → REG_RQPN_NPQ, REG_RQPN
  * init_queue_priority      → REG_TXDMA_PQ_MAP
  * init_tx_buffer_boundary  → 5 boundary writes
  * init_edca                → SIFS+EDCA+TSF
  * post_fw_mac_init         → final REG_CR has MACTXEN|MACRXEN
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wifit3.chips.rtl8821au.constants import (
    BIT_LD_RQPN,
    BIT_MACRXEN,
    BIT_MACTXEN,
    REG_BCN_CTRL,
    REG_BCNQ_BDNY,
    REG_CR,
    REG_EDCA_BE_PARAM,
    REG_HMETFR,
    REG_LLT_INIT,
    REG_MAR,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_TBTT_PROHIBIT,
    REG_TRXFF_BNDY,
    REG_TXDMA_PQ_MAP,
    REG_USTIME_EDCA,
    REG_USTIME_TSF,
    RTW_NET_NO_LINK,
    WLAN_TBTT_TIME,
)
from wifit3.chips.rtl8821au.fifo import set_trx_fifo_info
from wifit3.chips.rtl8821au.mac import (
    init_edca,
    init_queue_priority,
    init_queue_reserved_page,
    init_tx_buffer_boundary,
    init_wmac_setting,
    llt_init,
    post_fw_mac_init,
)


class MockTransport:
    """Records every register access; backed by a dict for reads."""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}    # current byte-stream model
        self.writes: list[tuple[str, int, int]] = []  # (op, addr, value)
        self.reads: list[tuple[str, int]] = []

    # 32-bit model: store every byte we write so reads are coherent.
    def _store(self, addr: int, data: list[int]) -> None:
        for i, b in enumerate(data):
            self.regs[addr + i] = b & 0xFF

    def _load(self, addr: int, n: int) -> int:
        out = 0
        for i in range(n):
            out |= self.regs.get(addr + i, 0) << (8 * i)
        return out

    def read8(self, addr: int) -> int:
        self.reads.append(("r8", addr))
        return self._load(addr, 1)

    def read16(self, addr: int) -> int:
        self.reads.append(("r16", addr))
        return self._load(addr, 2)

    def read32(self, addr: int) -> int:
        self.reads.append(("r32", addr))
        return self._load(addr, 4)

    def write8(self, addr: int, val: int) -> None:
        self.writes.append(("w8", addr, val & 0xFF))
        self._store(addr, [val & 0xFF])

    def write16(self, addr: int, val: int) -> None:
        self.writes.append(("w16", addr, val & 0xFFFF))
        self._store(addr, [val & 0xFF, (val >> 8) & 0xFF])

    def write32(self, addr: int, val: int) -> None:
        self.writes.append(("w32", addr, val & 0xFFFFFFFF))
        if addr == REG_LLT_INIT:
            # HW clears the request bits (30-31) the instant the write commits.
            val &= 0x3FFFFFFF
        self._store(addr, [
            val & 0xFF, (val >> 8) & 0xFF,
            (val >> 16) & 0xFF, (val >> 24) & 0xFF,
        ])

    def write8_set(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) | mask)

    def write8_clr(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) & ~mask & 0xFF)

    def write32_set(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) | mask)

    def write32_clr(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) & (~mask & 0xFFFFFFFF))

    def write32_mask(self, addr: int, mask: int, value: int) -> None:
        cur = self.read32(addr)
        shift = (mask & -mask).bit_length() - 1
        new = (cur & ~mask) | ((value << shift) & mask)
        self.write32(addr, new & 0xFFFFFFFF)

    def writes_to(self, addr: int) -> list[tuple[str, int, int]]:
        return [w for w in self.writes if w[1] == addr]


def _no_llt_read_pending(t: MockTransport) -> None:
    """Pre-seed REG_LLT_INIT to look 'done' for the LLT poll path."""
    t.regs[REG_LLT_INIT] = 0
    t.regs[REG_LLT_INIT + 1] = 0
    t.regs[REG_LLT_INIT + 2] = 0
    t.regs[REG_LLT_INIT + 3] = 0


# ---------------------------------------------------------------------------
# fifo / set_trx_fifo_info
# ---------------------------------------------------------------------------

def test_set_trx_fifo_info_for_8821a():
    fc = set_trx_fifo_info()
    assert fc.txff_pg_num == 256
    assert fc.rsvd_drv_pg_num == 8
    assert fc.rsvd_pg_num == 8
    assert fc.acq_pg_num == 248
    assert fc.rsvd_boundary == 248


# ---------------------------------------------------------------------------
# llt_init
# ---------------------------------------------------------------------------

def test_llt_init_writes_256_entries():
    t = MockTransport()
    _no_llt_read_pending(t)
    llt_init(t, boundary=248)
    llt_writes = t.writes_to(REG_LLT_INIT)
    # 256 LLT entries × 1 write each (poll-reads succeed first try in mock)
    assert len(llt_writes) == 256


def test_llt_init_endpoints():
    """Verify the special-cased entries: boundary-1 → 0xFF, last → boundary."""
    t = MockTransport()
    _no_llt_read_pending(t)
    llt_init(t, boundary=248)
    write_data = [w[2] for w in t.writes_to(REG_LLT_INIT)]
    # write encoding: BIT(30) | (addr<<8) | data
    def decode(v):
        return ((v >> 8) & 0xFF, v & 0xFF)
    decoded = [decode(v) for v in write_data]
    # First 247 entries chain forward: (i, i+1)
    for i in range(247):
        assert decoded[i] == (i, i + 1), f"entry {i} miswritten"
    # Entry 247 → 0xFF
    assert decoded[247] == (247, 0xFF)
    # Entries 248..254 chain forward
    for j, i in enumerate(range(248, 255)):
        assert decoded[248 + j] == (i, i + 1)
    # Last (255) → boundary (248)
    assert decoded[-1] == (255, 248)


# ---------------------------------------------------------------------------
# init_queue_reserved_page
# ---------------------------------------------------------------------------

def test_init_queue_reserved_page_values():
    t = MockTransport()
    fifo = set_trx_fifo_info()
    init_queue_reserved_page(t, fifo)

    # REG_RQPN_NPQ:  n=0, e=0  → 0
    npq_writes = t.writes_to(REG_RQPN_NPQ)
    assert len(npq_writes) == 1
    assert npq_writes[0] == ("w32", REG_RQPN_NPQ, 0)

    # REG_RQPN:  BIT_LD_RQPN | h=8 | l=0<<8 | pubq=239<<16
    # pubq = 248 - 8 - 1 = 239
    expected = BIT_LD_RQPN | 8 | (0 << 8) | (239 << 16)
    rqpn_writes = t.writes_to(REG_RQPN)
    assert len(rqpn_writes) == 1
    assert rqpn_writes[0] == ("w32", REG_RQPN, expected)


# ---------------------------------------------------------------------------
# init_queue_priority
# ---------------------------------------------------------------------------

def test_init_queue_priority_pq_map():
    """Verify the lane mapping word for USB-2-bulkout 8821A."""
    t = MockTransport()
    # Seed nonzero low 3 bits to verify they're preserved.
    t._store(REG_TXDMA_PQ_MAP, [0x05, 0x00])
    init_queue_priority(t)

    pq_writes = t.writes_to(REG_TXDMA_PQ_MAP)
    assert len(pq_writes) == 1
    val = pq_writes[0][2]
    # Low 3 bits preserved
    assert val & 0x7 == 0x5
    # Mapping bits: HI=NORMAL(2)@14, MG=NORMAL(2)@12, BK=LOW(1)@10,
    #               BE=LOW(1)@8, VI=EXTRA(0)@6, VO=HIGH(3)@4
    expected_map = (2 << 14) | (2 << 12) | (1 << 10) | (1 << 8) | (0 << 6) | (3 << 4)
    assert val & 0xFFF0 == expected_map


# ---------------------------------------------------------------------------
# init_tx_buffer_boundary
# ---------------------------------------------------------------------------

def test_init_tx_buffer_boundary_writes_five_regs():
    t = MockTransport()
    init_tx_buffer_boundary(t, set_trx_fifo_info())
    addrs = sorted({w[1] for w in t.writes if w[0] == "w8"})
    # 0x0114 (REG_TRXFF_BNDY), 0x0209 (REG_DWBCN0_CTRL+1),
    # 0x0424 (REG_BCNQ_BDNY), 0x0425 (REG_MGQ_BDNY), 0x045D (REG_WMAC_LBK_BF_HD)
    assert addrs == [0x0114, 0x0209, 0x0424, 0x0425, 0x045D]
    for _, _, val in [w for w in t.writes if w[0] == "w8"]:
        assert val == 248


# ---------------------------------------------------------------------------
# init_edca / init_wmac_setting
# ---------------------------------------------------------------------------

def test_init_edca_writes():
    t = MockTransport()
    init_edca(t)
    addrs_w32 = [w[1] for w in t.writes if w[0] == "w32"]
    assert REG_EDCA_BE_PARAM in addrs_w32
    addrs_w8 = [w[1] for w in t.writes if w[0] == "w8"]
    assert REG_USTIME_TSF in addrs_w8
    assert REG_USTIME_EDCA in addrs_w8


def test_init_wmac_setting_sets_mar_to_all_ones():
    t = MockTransport()
    init_wmac_setting(t)
    mar_writes = [w for w in t.writes if w[0] == "w32" and w[1] in (REG_MAR, REG_MAR + 4)]
    assert len(mar_writes) == 2
    for _, _, val in mar_writes:
        assert val == 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Full post_fw_mac_init — golden invariants
# ---------------------------------------------------------------------------

def test_post_fw_mac_init_sets_mactxen_macrxen():
    """End state must include MACTXEN|MACRXEN at byte 0 of REG_CR."""
    t = MockTransport()
    _no_llt_read_pending(t)
    # Seed REG_CR so the MACTXEN|MACRXEN bits start clear.
    t._store(REG_CR, [0, 0, 0, 0])
    post_fw_mac_init(t, set_trx_fifo_info())
    final_cr_byte0 = t.regs.get(REG_CR, 0)
    assert final_cr_byte0 & BIT_MACTXEN
    assert final_cr_byte0 & BIT_MACRXEN


def test_post_fw_mac_init_sets_monitor_net_type_no_link():
    """Net type (REG_CR bits 16-17) must end as NO_LINK so the MAC captures
    BOTH directions. Left at MGD_LINKED (the kernel init default) it accepts
    only FromDS (AP→client), dropping the M2/M4 client→AP EAPOL needed to
    complete a 4-way handshake."""
    t = MockTransport()
    _no_llt_read_pending(t)
    post_fw_mac_init(t, set_trx_fifo_info())
    net_type = t.regs.get(REG_CR + 2, 0) & 0x03  # bits 16-17 of REG_CR
    assert net_type == RTW_NET_NO_LINK


def test_post_fw_mac_init_writes_hmetfr_first():
    """First write should target REG_HMETFR (rtw88xxa.c:1081)."""
    t = MockTransport()
    _no_llt_read_pending(t)
    post_fw_mac_init(t, set_trx_fifo_info())
    first_write = t.writes[0]
    assert first_write == ("w8", REG_HMETFR, 0x0F)


def test_post_fw_mac_init_tbtt_prohibit_value():
    """REG_TBTT_PROHIBIT lower 20 bits must end as WLAN_TBTT_TIME (0x6404)."""
    t = MockTransport()
    _no_llt_read_pending(t)
    post_fw_mac_init(t, set_trx_fifo_info())
    final = t.regs.get(REG_TBTT_PROHIBIT, 0) | (
        t.regs.get(REG_TBTT_PROHIBIT + 1, 0) << 8
    ) | (t.regs.get(REG_TBTT_PROHIBIT + 2, 0) << 16)
    assert final & 0xFFFFF == WLAN_TBTT_TIME


def test_post_fw_mac_init_bcn_ctrl_no_btcoex_value():
    """REG_BCN_CTRL with btcoex=False must be (BIT_DIS_TSF_UDT<<8 | BIT_DIS_TSF_UDT) = 0x1010."""
    t = MockTransport()
    _no_llt_read_pending(t)
    post_fw_mac_init(t, set_trx_fifo_info())
    bcn = t.regs.get(REG_BCN_CTRL, 0) | (t.regs.get(REG_BCN_CTRL + 1, 0) << 8)
    assert bcn == 0x1010
