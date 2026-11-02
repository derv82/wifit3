"""RTL8187B paths, op order and values checked against rtl818x-source-v6.18/rtl8187."""
from __future__ import annotations

from typing import Sequence

import pytest

import wifit3.chips.rtl8187.probe as probe_mod
import wifit3.chips.rtl8187.rtl8225 as rf
from wifit3.chips.rtl8187.constants import (
    HwRev,
    REG_GPIO0,
    REG_TX_CONF,
)
from wifit3.chips.rtl8187.rtl8225 import RfVariant


class IdxRecordingTransport:
    """Dict-backed register space logging (op, addr, value, idx) for every access."""

    def __init__(self, preset: dict[int, int] | None = None) -> None:
        self.regs: dict[int, int] = dict(preset or {})
        self.ops: list[tuple[str, int, int, int]] = []

    def _load(self, addr: int, n: int) -> int:
        return sum(self.regs.get(addr + i, 0) << (8 * i) for i in range(n))

    def _store(self, addr: int, val: int, n: int) -> None:
        for i in range(n):
            self.regs[addr + i] = (val >> (8 * i)) & 0xFF

    def read8(self, addr: int, idx: int = 0) -> int:
        val = self._load(addr, 1)
        if addr == 0xFF37 and val & 0x10:          # CMD_RESET self-clears
            val &= ~0x10
            self._store(addr, val, 1)
        if addr == 0xFF50 and val == 0x40:         # EEPROM_CMD_LOAD completes
            val = 0
            self._store(addr, val, 1)
        self.ops.append(("r8", addr, val, idx))
        return val

    def read16(self, addr: int, idx: int = 0) -> int:
        val = self._load(addr, 2)
        self.ops.append(("r16", addr, val, idx))
        return val

    def read32(self, addr: int, idx: int = 0) -> int:
        val = self._load(addr, 4)
        self.ops.append(("r32", addr, val, idx))
        return val

    def write8(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w8", addr, val & 0xFF, idx))
        self._store(addr, val, 1)

    def write16(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w16", addr, val & 0xFFFF, idx))
        self._store(addr, val, 2)

    def write32(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w32", addr, val & 0xFFFFFFFF, idx))
        self._store(addr, val, 4)

    def write_bytes(self, addr: int, data: Sequence[int]) -> None:
        for i, b in enumerate(data):
            self.regs[addr + i] = b & 0xFF


# ---- probe (dev.c:1431-1648) --------------------------------------------------------------
def _probe_b(monkeypatch, product_id: int, ffe1: int, select_gpio: int = 0x0000):
    words: list[int] = []

    class FakeEeprom:
        def __init__(self, t, width):
            pass

        def read(self, word: int) -> int:
            words.append(word)
            return select_gpio if word == 0x3B else 0x1234

        def multiread(self, word: int, n: int) -> list[int]:
            return [self.read(word + i) for i in range(n)]

    rf_spi: list[tuple] = []
    masks: list[int] = []
    monkeypatch.setattr(probe_mod, "Eeprom93cx6", FakeEeprom)
    monkeypatch.setattr(probe_mod, "eeprom_width", lambda t: 6)
    monkeypatch.setattr(probe_mod, "is_radio_enabled", lambda t, mask: (masks.append(mask), True)[1])
    monkeypatch.setattr(rf, "rtl8225_write", lambda *a: rf_spi.append(a))
    monkeypatch.setattr(rf, "rtl8225_read", lambda *a: rf_spi.append(a))
    t = IdxRecordingTransport({0xFFE1: ffe1})
    return probe_mod.probe(t, product_id, True), t, words, rf_spi, masks


@pytest.mark.parametrize("ffe1, name, hw_rev", [
    (0, "RTL8187BvB", HwRev.RTL8187BvB),
    (1, "RTL8187BvD", HwRev.RTL8187BvD),
    (2, "RTL8187BvE", HwRev.RTL8187BvE),
    (7, "RTL8187BvB (default)", HwRev.RTL8187BvB),
])
def test_probe_b_id_reads_hw_rev_from_ffe1_not_tx_conf(monkeypatch, ffe1, name, hw_rev):
    pr, t, _, rf_spi, _ = _probe_b(monkeypatch, 0x8189, ffe1)
    assert ("r8", 0xFFE1, ffe1, 0) in t.ops
    assert not any(op[1] == REG_TX_CONF for op in t.ops)
    assert pr.chip.is_rtl8187b and pr.chip.name == name and pr.chip.hw_rev is hw_rev
    assert pr.setup.hw_rev is hw_rev
    assert pr.setup.variant is RfVariant.RTL8225Z2_B
    assert rf_spi == []                                   # detect_rf: no SPI probe on B


def test_probe_b_reads_ffe1_after_closing_the_eeprom_window(monkeypatch):
    _, t, _, _, _ = _probe_b(monkeypatch, 0x8189, 0)
    assert t.ops[-2:] == [("w8", 0xFF50, 0x00, 0), ("r8", 0xFFE1, 0, 0)]


@pytest.mark.parametrize("product_id, select_gpio, mask, tail", [
    (0x8198, 0x0100, 0x4, [0x1B, 0x0A, 0x1C, 0x3B, 0x3F]),
    (0x8197, 0x00FF, 0x2, [0x1B, 0x0A, 0x1C, 0x3B, 0x3F]),
    (0x8189, 0x0100, 0x2, [0x1B, 0x0A, 0x1C, 0x3F]),
])
def test_probe_b_rfkill_mask_from_eeprom_select_gpio(monkeypatch, product_id, select_gpio, mask, tail):
    """dev.c:1607-1612: only 0x8197/0x8198 read EEPROM 0x3B; a high byte selects mask 0x4."""
    pr, _, words, _, masks = _probe_b(monkeypatch, product_id, 0, select_gpio)
    assert words[-len(tail):] == tail
    assert pr.rfkill_mask == mask and masks == [mask]
    assert pr.power.hw_value[10:] == (0x34, 0x34, 0x34, 0x12)


def test_is_radio_enabled_uses_the_probed_mask():
    from wifit3.chips.rtl8187.rfkill import is_radio_enabled

    t = IdxRecordingTransport({REG_GPIO0: 0xFF, REG_GPIO0 + 1: 0x04})
    assert is_radio_enabled(t, 0x4) is True
    assert t.ops[1] == ("w8", REG_GPIO0, 0xFB, 0)


# ---- init (dev.c:570-608, 739-881, 939-971) ----------------------------------------------
_DEV_C = (__import__("pathlib").Path(__file__).resolve().parents[3]
          / "driver_sources" / "rtl818x-source-v6.18" / "rtl8187" / "dev.c")


def test_rtl8187b_reg_table_matches_dev_c():
    if not _DEV_C.is_file():
        pytest.skip("driver_sources/ is gitignored, absent in a clone")
    import re

    from wifit3.chips.rtl8187.mac import rtl8187b_reg_table

    src = _DEV_C.read_text()
    body = src[src.index("rtl8187b_reg_table[][3] = {"):src.index("static int rtl8187b_init_hw")]
    rows = [(int(a, 16), int(v, 16), int(i))
            for a, v, i in re.findall(r"\{(0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+), (\d)\}", body)]
    assert rows == list(rtl8187b_reg_table)


def _anaparam_ops(on: bool) -> list[tuple]:
    a, a2 = (0x45090658, 0x727F3F52) if on else (0x55480658, 0x72003F50)
    return [("w8", 0xFF50, 0xC0, 0), ("r8", 0xFF59, 0x00, 0), ("w8", 0xFF59, 0x40, 0),
            ("w32", 0xFF54, a, 0), ("w32", 0xFF60, a2, 0), ("w8", 0xFFEE, 0x00, 0),
            ("w8", 0xFF59, 0x00, 0), ("w8", 0xFF50, 0x00, 0)]


def test_set_anaparam_b_writes_b_values_and_anaparam3a():
    from wifit3.chips.rtl8187 import mac

    for on in (True, False):
        t = IdxRecordingTransport()
        mac.set_anaparam(t, rfon=on, is_rtl8187b=True)
        assert t.ops == _anaparam_ops(on)


def test_rtl8187b_init_hw_op_order(monkeypatch):
    from wifit3.chips.rtl8187 import mac

    monkeypatch.setattr(mac.time, "sleep", lambda *_a: None)
    t = IdxRecordingTransport()
    mac.rtl8187b_init_hw(t, rf_init=lambda _t: _t.ops.append(("rf_init", 0, 0, 0)))

    expected = _anaparam_ops(True) + [
        # 0xFF62 is byte 2 of the ANAPARAM2 dword just written (0x727F3F52)
        ("w8", 0xFF61, 0x10, 0), ("r8", 0xFF62, 0x7F, 0),
        ("w8", 0xFF62, 0x5F, 0), ("w8", 0xFF62, 0x7F, 0),
        # rtl8187_cmd_reset
        ("r8", 0xFF37, 0x00, 0), ("w8", 0xFF37, 0x10, 0), ("r8", 0xFF37, 0x00, 0),
        ("w8", 0xFF50, 0x40, 0), ("r8", 0xFF50, 0x00, 0),
    ] + _anaparam_ops(True) + [
        ("w16", 0xFF34, 0x0FFF, 0),
        ("r8", 0xFFBC, 0x00, 0), ("w8", 0xFFBC, 0x02, 0),
        ("w16", 0xFFE0, 0x0FFF, 1), ("w8", 0xFFE2, 0x00, 1), ("w16", 0xFFD4, 0xFFFF, 1),
        ("w8", 0xFF50, 0xC0, 0), ("r8", 0xFF52, 0x00, 0), ("w8", 0xFF52, 0x80, 0),
        ("w8", 0xFF50, 0x00, 0),
        ("w8", 0xFFB0, 0x00, 0),
    ] + [("w8", a | 0xFF00, v, i) for a, v, i in mac.rtl8187b_reg_table] + [
        ("w16", 0xFFE8, 0xFA50, 0), ("w16", 0xFFE2, 0x0000, 0),
        ("w32", 0xFFF0, 0, 1), ("w32", 0xFFF4, 0, 1), ("w8", 0xFFF8, 0, 1),
        ("w32", 0xFF8C, 0x00004001, 0),
        ("w16", 0xFF72, 0x569A, 2),
        ("w16", 0xFF80, 0x0480, 0), ("w16", 0xFF84, 0x2488, 0), ("w16", 0xFF82, 0x1FFF, 0),
        ("rf_init", 0, 0, 0),
        ("w8", 0xFF37, 0x0C, 0), ("w16", 0xFF3C, 0xFFFF, 0),
        ("w8", 0xFE41, 0xF4, 0), ("w8", 0xFE40, 0x00, 0), ("w8", 0xFE42, 0x00, 0),
        ("w8", 0xFE42, 0x01, 0), ("w8", 0xFE40, 0x0F, 0), ("w8", 0xFE42, 0x00, 0),
        ("w8", 0xFE42, 0x01, 0),
    ]
    # 0xFFDB was written by the reg table (0xDB, 0x00), so the RMW reads 0x00 back.
    expected += [
        ("r8", 0xFFDB, 0x00, 0), ("w8", 0xFFDB, 0x04, 0),
        ("w16", 0xFF72, 0x59FA, 3), ("w16", 0xFF74, 0x59D2, 3), ("w16", 0xFF76, 0x59D2, 3),
        ("w16", 0xFF78, 0x19FA, 3), ("w16", 0xFF7A, 0x19FA, 3), ("w16", 0xFF7C, 0x00D0, 3),
        ("w8", 0xFF61, 0x00, 0), ("w8", 0xFF80, 0x0F, 1), ("w8", 0xFF83, 0x03, 1),
        ("w8", 0xFFDA, 0x10, 0), ("w8", 0xFF4D, 0x08, 2),
        ("w32", 0xFF94, 0x0600321B, 0),
        ("w16", 0xFFEC, 0x0800, 1),
        ("w8", 0xFFBF, 0x00, 0),
        ("w8", 0xFF58, 0x10, 0),
    ]
    assert t.ops == expected


def test_start_b_branch_writes_rx_conf_tx_agc_and_hw_seqnum_tx_conf():
    from wifit3.chips.rtl8187 import mac

    t = IdxRecordingTransport({0xFF9C: 0xFF})
    rx_conf = mac.start(t, is_rtl8187b=True)
    assert rx_conf == 0x9094FC0A
    assert t.ops == [
        ("w32", 0xFF44, 0x9094FC0A, 0),
        ("r8", 0xFF9C, 0xFF, 0), ("w8", 0xFF9C, 0xF8, 0),
        ("w32", 0xFF40, (1 << 30) | (1 << 28) | (7 << 8) | 7 | (7 << 21), 0),
    ]


def test_cold_bring_up_b_runs_rtl8187b_init_hw_then_start_then_monitor(monkeypatch):
    from wifit3.chips.rtl8187 import mac

    calls: list[str] = []
    monkeypatch.setattr(mac, "init_hw", lambda t, rf_init: calls.append("init_hw"))
    monkeypatch.setattr(mac, "rtl8187b_init_hw", lambda t, rf_init: calls.append("b_init_hw"))
    t = IdxRecordingTransport()
    rx_conf = mac.cold_bring_up(t, lambda _t: None, is_rtl8187b=True)
    assert calls == ["b_init_hw"]
    assert rx_conf == 0x909CFC0B
    assert t.ops[-1] == ("w32", 0xFF44, 0x909CFC0B, 0)


def test_stop_b_uses_b_anaparam_off():
    from wifit3.chips.rtl8187 import mac

    t = IdxRecordingTransport()
    mac.stop(t, lambda _t: None, is_rtl8187b=True)
    assert t.ops[3:11] == _anaparam_ops(False)


@pytest.mark.parametrize("val, expected", [
    ((1 << 30) | (0x123 << 16) | (1 << 15) | 3, (3, True, 0x123)),
    ((1 << 30) | (0xABC << 16) | 0, (0, False, 0xABC)),
    (0, None),                                       # cmd_type 0: tx beacon interrupt
])
def test_rtl8187b_status_cb_decodes_tx_close_descriptor(val, expected):
    from wifit3.chips.rtl8187.tx import rtl8187b_status_cb

    status = rtl8187b_status_cb(val.to_bytes(8, "little"))
    got = None if status is None else (status.pkt_rc, status.tok, status.seq_no)
    assert got == expected


# ---- RF (rtl8225.c:674-979) ---------------------------------------------------------------
def _record_rf(monkeypatch, t: IdxRecordingTransport) -> None:
    monkeypatch.setattr(rf, "rtl8225_write",
                        lambda _t, addr, data, asic_rev: t.ops.append(("rf", addr, data, asic_rev)))
    monkeypatch.setattr(rf, "write_phy", lambda _t, addr, data: t.ops.append(("phy", addr, data, 0)))
    monkeypatch.setattr(rf.time, "sleep", lambda *_a: None)


def _b_setup(hw_rev: HwRev) -> rf.RfSetup:
    return rf.RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2_B, hw_rev=hw_rev)


def test_rtl8225z2_b_rf_init_op_order(monkeypatch):
    t = IdxRecordingTransport()
    _record_rf(monkeypatch, t)
    rf.build_rf_init(t, _b_setup(HwRev.RTL8187BvE), rf.TxPower(hw_value=(0,) * 14))(t)

    rfw = [(0x0, 0x0B7), (0x1, 0xEE0), (0x2, 0x44D), (0x3, 0x441), (0x4, 0x8C3), (0x5, 0xC72),
           (0x6, 0x0E6), (0x7, 0x82A), (0x8, 0x03F), (0x9, 0x335), (0xa, 0x9D4), (0xb, 0x7BB),
           (0xc, 0x850), (0xd, 0xCDF), (0xe, 0x02B), (0xf, 0x114), (0x0, 0x1B7)]
    for i, val in enumerate(rf.rtl8225z2_rxgain):
        rfw += [(0x1, i + 1), (0x2, val)]
    rfw += [(0x3, 0x080), (0x5, 0x004), (0x0, 0x0B7), (0x2, 0xC4D), (0x2, 0x44D), (0x0, 0x2BF)]
    expected = [("rf", a, d, 1) for a, d in rfw]
    expected += [("w8", 0xFF9D, 0x03, 0), ("w8", 0xFF9E, 0x07, 0), ("w8", 0xFF9F, 0x03, 0)]
    expected += [("phy", 0x80, 0x12, 0)]
    for i, val in enumerate(rf.rtl8225z2_agc):
        expected += [("phy", 0xF, val, 0), ("phy", 0xE, 0x80 + i, 0), ("phy", 0xE, 0, 0)]
    expected += [("phy", 0x80, 0x10, 0)]
    expected += [("phy", i, val, 0) for i, val in enumerate(rf.rtl8225z2_ofdm)]
    expected += [("phy", 0x97, 0x46, 0), ("phy", 0xa4, 0xb6, 0), ("phy", 0x85, 0xfc, 0),
                 ("phy", 0xc1, 0x10088, 0)]
    assert t.ops == expected


@pytest.mark.parametrize("hw_rev, hv, base, channel, cck_row, cck_gain, ofdm_gain, ofdm_87_89", [
    # BvB: cck = 5 + 0 + 6 = 11 -> row 1; ofdm = 5 + 2 + 3 = 10 -> 0x60
    (HwRev.RTL8187BvB, 0x55, 0x36, 1, 1, 11 << 1, 10 << 1, 0x60),
    # BvE: cck = 5 + 7 + 6 = 18 -> row 3; ofdm = 5 + 10 + 3 = 18 -> 0x50
    (HwRev.RTL8187BvE, 0x55, 0x36, 1, 3, 18 << 1, 18 << 1, 0x50),
    # BvD: cck = 0 + 7 = 7 -> row 1; ofdm = 15 + 10 = 25 -> 0x50
    (HwRev.RTL8187BvD, 0xF0, 0x00, 6, 1, 7 << 1, 25 << 1, 0x50),
    # BvD: cck = 2 + 7 + 1 = 10 -> row 1; ofdm = 3 + 10 + 2 = 15 -> 0x54
    (HwRev.RTL8187BvD, 0x32, 0x21, 6, 1, 10 << 1, 15 << 1, 0x54),
    # BvB, ch14 table: cck = 0 -> row 0; ofdm = 0 + 2 = 2 -> 0x60
    (HwRev.RTL8187BvB, 0x00, 0x00, 14, 0, 0, 2 << 1, 0x60),
    # cck clamps at 35: 15 + 7 + 15 = 37 -> 35, row 3; ofdm = 15 + 10 + 15 = 40 -> 35
    (HwRev.RTL8187BvE, 0xFF, 0xFF, 1, 3, 35 << 1, 35 << 1, 0x50),
])
def test_rtl8225z2_b_rf_set_tx_power(monkeypatch, hw_rev, hv, base, channel, cck_row, cck_gain,
                                     ofdm_gain, ofdm_87_89):
    t = IdxRecordingTransport()
    _record_rf(monkeypatch, t)
    power = rf.TxPower(hw_value=(hv,) * 14, base=base)
    rf.set_tx_power(t, _b_setup(hw_rev), channel, power)

    tbl = rf.rtl8225z2_tx_power_cck_ch14 if channel == 14 else rf.rtl8225z2_tx_power_cck
    expected = [("phy", 0x44 + i, 0x10000 | tbl[cck_row * 8 + i], 0) for i in range(8)]
    expected += [("w8", 0xFF9D, cck_gain, 0), ("w8", 0xFF9E, ofdm_gain, 0),
                 ("phy", 0x87, ofdm_87_89, 0), ("phy", 0x89, ofdm_87_89, 0)]
    assert t.ops == expected


def test_config_channel_on_z2_b_runs_the_b_tx_power_then_rf7(monkeypatch):
    from wifit3.chips.rtl8187 import chan

    monkeypatch.setattr(chan.time, "sleep", lambda *_a: None)
    calls: list[tuple] = []
    monkeypatch.setattr(rf, "rtl8225z2_b_rf_set_tx_power",
                        lambda t, ch, power, hw_rev: calls.append(("b_tx_power", ch, hw_rev)))
    monkeypatch.setattr(chan, "rtl8225_write",
                        lambda t, addr, data, asic_rev: calls.append(("rf", addr, data)))
    t = IdxRecordingTransport()
    chan.config_channel(t, _b_setup(HwRev.RTL8187BvD), 11, rf.TxPower(hw_value=(0,) * 14))
    assert calls == [("b_tx_power", 11, HwRev.RTL8187BvD), ("rf", 0x7, rf.rtl8225_chan[10])]


# ---- TX / RX (dev.c:227-412) -------------------------------------------------------------
def _deauth() -> bytes:
    from wifit3.chips.rtl8187.tx import build_deauth

    return build_deauth(bytes.fromhex("020000000002"), bytes.fromhex("020000000001"))


def test_rtl8187b_tx_hdr_layout():
    import struct

    from wifit3.chips.rtl8187.tx import build_rtl8187b_tx_hdr

    hdr = build_rtl8187b_tx_hdr(26)
    assert len(hdr) == 32
    flags, rts, length, unused_1, unused_2, tx_duration, unused_3, retry = struct.unpack(
        "<IHHIHHII", hdr[:24])
    assert flags == 26 | (1 << 15)                    # len | NO_ENC | 1 Mbps (hw_value 0)
    assert (rts, length, unused_1, unused_2, unused_3) == (0, 0, 0, 0, 0)
    # util.c:165-168 over skb->len after the push (26 + 32): 10 + 192 + ceil(8 * 62 * 10 / 10)
    assert tx_duration == 10 + 192 + 8 * (26 + 32 + 4)
    assert retry == (7 - 1) << 8
    assert hdr[24:] == bytes(8)


def test_ieee80211_frame_duration_ofdm_branch():
    from wifit3.chips.rtl8187.tx import ieee80211_frame_duration

    # 54 Mbps, 100 B: 36 + 4 * ceil((16 + 8 * 104 + 6) * 10 / 2160) = 36 + 4 * 4
    assert ieee80211_frame_duration(100, 540, 1, False) == 52
    assert ieee80211_frame_duration(100, 20, 0, True) == 10 + 96 + 416


def _qos_data(tid: int, four_addr: bool = False) -> bytes:
    fc1 = 0x03 if four_addr else 0x01
    hdr = bytes([0x88, fc1]) + bytes(22) + (bytes(6) if four_addr else b"")
    return hdr + bytes([tid, 0x00]) + bytes(8)


@pytest.mark.parametrize("frame, ep", [
    (bytes([0xC0, 0x00]) + bytes(24), 12),            # mgmt (deauth)
    (bytes([0xD4, 0x00]) + bytes(8), 6),              # control: priority 7 -> AC_VO
    (bytes([0x08, 0x01]) + bytes(30), 5),             # non-QoS data: priority 0 -> AC_BE
    (_qos_data(6), 6),                                # TID 6 -> AC_VO
    (_qos_data(5), 7),                                # TID 5 -> AC_VI
    (_qos_data(1), 4),                                # TID 1 -> AC_BK
    (_qos_data(0), 5),                                # TID 0 -> AC_BE
    (_qos_data(4, four_addr=True), 7),                # 4-addr: QoS control at 30
])
def test_rtl8187b_tx_ep(frame, ep):
    from wifit3.chips.rtl8187.tx import rtl8187b_tx_ep

    assert rtl8187b_tx_ep(frame) == ep


def test_inject_frame_b_writes_32_byte_header_to_the_frame_endpoint():
    from unittest.mock import MagicMock

    from wifit3.chips.rtl8187.tx import inject_frame

    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)
    frame = bytes([0x08, 0x01]) + bytes(30)           # 32 + 32 = 64 B -> ZLP on a 64 B ep
    inject_frame(dev, frame, {12: 512, 6: 512, 7: 512, 5: 64, 4: 512}, True)
    ep, payload, _ = dev.write.call_args_list[0].args
    assert ep == 5 and len(payload) == 64 and payload[32:] == frame
    assert dev.write.call_args_list[1].args[:2] == (5, b"")


def test_parse_rx_urb_b_decodes_the_20_byte_trailer():
    import struct

    from wifit3.chips.rtl8187.rx import parse_rx_urb

    body = bytes(range(30))
    hdr = struct.pack("<IQBBBBHbB", (len(body) + 4) | (3 << 20), 0x1122334455667788,
                      0x10, 0x80, 0x50, 0, 0, -3, 0)
    rx = parse_rx_urb(body + b"FCS!" + hdr, True)
    assert rx.mpdu == body
    assert rx.rssi_dbm == 14 - 0x50 // 2
    assert rx.antenna == 1 and rx.rate_idx == 3 and rx.mac_time == 0x1122334455667788
    assert parse_rx_urb(bytes(19), True) is None


def test_stamp_tx_seq_is_a_no_op_on_b():
    from unittest.mock import MagicMock

    from wifit3.chips.rtl8187.driver import RTL8187Driver
    from wifit3.chips.rtl8187.mac import ChipVariant

    d = RTL8187Driver(MagicMock())
    d.chip_variant = ChipVariant(hwver_raw=None, name="RTL8187BvE", is_rtl8187b=True,
                                 hw_rev=HwRev.RTL8187BvE)
    assert d._stamp_tx_seq(_deauth()) == _deauth()
    assert d._tx_seqno == 0
