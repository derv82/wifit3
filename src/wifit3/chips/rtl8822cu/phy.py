"""RTL8822C BB/AGC/RF initialization tables and register protocol.

The four bundled binary tables are verbatim little-endian ``u32`` pairs
extracted from Realtek's GPL rtl88x2cu driver.  Their control records use the
same condition language as ``halhwimg8822c_{bb,rf}.c``; execution selects the
board's EFUSE RFE type before issuing any hardware writes.
"""
from __future__ import annotations

import struct
import time
from collections.abc import Iterator
from pathlib import Path

from .transport import RTL8822CUTransport

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_TABLES = {
    "agc": _ASSET_DIR / "rtl8822c_agc.bin",
    "bb": _ASSET_DIR / "rtl8822c_bb.bin",
    "rf_a": _ASSET_DIR / "rtl8822c_rf_a.bin",
    "rf_b": _ASSET_DIR / "rtl8822c_rf_b.bin",
}

_PARA_IF = 0x8
_PARA_ELSE_IF = 0x9
_PARA_ELSE = 0xA
_PARA_END = 0xB
_PARA_CHECK = 0x4
_CUT_DONT_CARE = 0xF
_RFE_DONT_CARE = 0xFF
_RF_MASK = 0x000FFFFF


def load_table(name: str) -> tuple[tuple[int, int], ...]:
    """Load a table as immutable ``(address, value)`` pairs."""
    try:
        raw = _TABLES[name].read_bytes()
    except KeyError as exc:
        raise ValueError(f"unknown RTL8822C PHY table {name!r}") from exc
    if len(raw) % 8:
        raise ValueError(f"RTL8822C PHY table {name} has a partial pair")
    return tuple(struct.iter_unpack("<II", raw))


def _select_target(table: tuple[tuple[int, int], ...], cut: int, rfe_type: int) -> tuple[int, int]:
    """Mirror ``halbb_sel_headline`` and return (first data pair, target)."""
    headline_end = 0
    while headline_end < len(table) and table[headline_end][0] >> 28 == 0xF:
        headline_end += 1
    if not headline_end:
        return 0, 0
    candidates = [entry[0] & 0x0FFFFFFF for entry in table[:headline_end]]
    def _matches(entry: int, wanted: int) -> bool:
        return entry & 0x0F0000FF == wanted
    wanted = ((cut & 0xF) << 24) | (rfe_type & 0xFF)
    for candidate in candidates:
        if _matches(candidate, wanted):
            return headline_end, candidate
    wanted = (_CUT_DONT_CARE << 24) | (rfe_type & 0xFF)
    for candidate in candidates:
        if _matches(candidate, wanted):
            return headline_end, candidate
    matching_rfe = [item for item in candidates if (item & 0xFF) == (rfe_type & 0xFF)]
    if matching_rfe:
        return headline_end, max(matching_rfe, key=lambda item: (item >> 24) & 0xF)
    generic = [item for item in candidates if (item & 0xFF) == _RFE_DONT_CARE]
    if generic:
        return headline_end, max(generic, key=lambda item: (item >> 24) & 0xF)
    raise ValueError(f"RTL8822C PHY table has no branch for cut={cut}, RFE={rfe_type}")


def selected_writes(table: tuple[tuple[int, int], ...], *, cut: int,
                    rfe_type: int) -> Iterator[tuple[int, int]]:
    """Yield the data records chosen by the Realtek table condition machine."""
    start, target = _select_target(table, cut, rfe_type)
    is_matched = True
    found_target = False
    cfg_parameter = 0
    for address, value in table[start:]:
        kind = address >> 28
        if kind in (_PARA_IF, _PARA_ELSE_IF):
            cfg_parameter = address & 0x0FFFFFFF
        elif kind == _PARA_ELSE:
            is_matched = False
            if not found_target:
                raise ValueError("RTL8822C PHY table has no matching conditional branch")
        elif kind == _PARA_END:
            is_matched = True
            found_target = False
        elif kind == _PARA_CHECK:
            if found_target:
                is_matched = False
            else:
                is_matched = cfg_parameter == target
                found_target = is_matched
        elif is_matched:
            yield address, value


def _write_bb(transport: RTL8822CUTransport, address: int, value: int) -> None:
    # The tables use f9..fe as delay opcodes, not BB register addresses.
    delays = {0xF9: 0.000001, 0xFA: 0.000005, 0xFB: 0.000050,
              0xFC: 0.001, 0xFD: 0.005, 0xFE: 0.050}
    if address in delays:
        time.sleep(delays[address])
    else:
        transport.write32(address, value)


def _write_rf(transport: RTL8822CUTransport, path: int, address: int, value: int) -> None:
    if address == 0xFFE:
        time.sleep(0.050)
    elif address == 0xFE:
        time.sleep(0.000100)
    elif address == 0xFFFF:
        time.sleep(0.000001)
    else:
        # RTL8822C direct RF window: path A=0x3c00, path B=0x4c00.
        transport.write32((0x3C00, 0x4C00)[path] + ((address & 0xFF) << 2), value & _RF_MASK)
        time.sleep(0.000001)


def initialize_phy(transport: RTL8822CUTransport, *, cut: int, rfe_type: int) -> None:
    """Load the board-selected AGC, BB, RF-A and RF-B initialization tables."""
    _phy_parameter_init(transport, post=False)
    for address, value in selected_writes(load_table("agc"), cut=cut, rfe_type=rfe_type):
        _write_bb(transport, address, value)
    for address, value in selected_writes(load_table("bb"), cut=cut, rfe_type=rfe_type):
        _write_bb(transport, address, value)
    for path, name in ((0, "rf_a"), (1, "rf_b")):
        for address, value in selected_writes(load_table(name), cut=cut, rfe_type=rfe_type):
            _write_rf(transport, path, address, value)
    _phy_parameter_init(transport, post=True)


def _write_bb_mask(transport: RTL8822CUTransport, address: int, mask: int, value: int) -> None:
    shift = (mask & -mask).bit_length() - 1
    current = transport.read32(address)
    transport.write32(address, (current & ~mask) | ((value << shift) & mask))


def _set_channel_mac(transport: RTL8822CUTransport, channel: int) -> None:
    """Mirror rtw_set_channel_mac for a fixed 20 MHz primary channel."""
    transport.write8(0x0483, 0x00)  # primary channel index 0, 20 MHz
    _write_bb_mask(transport, 0x0668, (1 << 7) | (1 << 8), 0)
    _write_bb_mask(transport, 0x0024, (1 << 20) | (1 << 21), 0)
    transport.write8(0x055C, 80)
    transport.write8(0x0638, 80)
    if channel > 14:
        transport.write8_set(0x0454, 1 << 7)
    else:
        transport.write8_clr(0x0454, 1 << 7)


def _apply_phy_changes(transport: RTL8822CUTransport) -> None:
    """Port ``phydm_bb_reset_8822c`` and ``phydm_igi_toggle_8822c``.

    The reset latches the BB configuration; toggling IGI then forces the
    hardware to submit its queued 3-wire RF command and leave RX idle mode.
    """
    transport.write32_set(0x0000, 1 << 16)
    transport.write32_clr(0x0000, 1 << 16)
    transport.write32_set(0x0000, 1 << 16)
    igi = transport.read32(0x1D70) & 0x7F
    _write_bb_mask(transport, 0x1D70, 0x7F, (igi - 2) & 0x7F)
    _write_bb_mask(transport, 0x1D70, 0x7F00, (igi - 2) & 0x7F)
    _write_bb_mask(transport, 0x1D70, 0x7F, igi)
    _write_bb_mask(transport, 0x1D70, 0x7F00, igi)


def _phy_parameter_init(transport: RTL8822CUTransport, *, post: bool) -> None:
    """Port ``config_phydm_parameter_init_8822c`` for normal operation.

    The pre/post pair brackets the PHY tables.  Without the post phase the
    CCK and OFDM blocks remain disabled, so 2.4 GHz beacons never reach RXDMA.
    """
    _write_bb_mask(transport, 0x180C, 0x3, 3)
    _write_bb_mask(transport, 0x180C, 1 << 28, 1)
    _write_bb_mask(transport, 0x410C, 0x3, 3)
    _write_bb_mask(transport, 0x410C, 1 << 28, 1)
    _write_bb_mask(transport, 0x1C3C, 0x3, 3 if post else 0)
    _apply_phy_changes(transport)


def set_channel_20mhz(transport: RTL8822CUTransport, channel: int) -> None:
    """Switch the RTL8822C radio and PHY to a 20 MHz primary channel.

    This is the RX-relevant path of the vendor's
    ``config_phydm_switch_channel_bw_8822c``.  In particular, 2.4 GHz needs
    its CCK receive chain and RF RXBB filter re-enabled after a 5 GHz hop.
    """
    legal = set(range(1, 15)) | {36, 40, 44, 48, 149, 153, 157, 161, 165}
    if channel not in legal:
        raise ValueError(f"unsupported RTL8822CU 20 MHz channel {channel}")
    # Build RF18 from the documented RTL8822C fields.  The USB direct-read
    # window may return the encoded address on this firmware, so preserving it
    # would carry stale channel bits into the next tune.
    rf18 = 0
    rf18 = (rf18 & ~((1 << 18) | (1 << 17) | (1 << 16) | (1 << 9) | (1 << 8)
                    | 0xFF | (1 << 13) | (1 << 12))) | channel
    is_2g = channel <= 14
    # RF18[13:12] = 0b11 is 20 MHz on RTL8822C.
    rf18 |= (1 << 13) | (1 << 12)
    if not is_2g:
        rf18 |= (1 << 16) | (1 << 8)
        if channel > 144:
            rf18 |= 1 << 18
        elif channel >= 80:
            rf18 |= 1 << 17

    _write_bb_mask(transport, 0x1C90, 1 << 8, 0)
    # config_phydm_switch_channel_bw_8822c updates the RXBB LUT on both RF
    # paths before latching the channel synthesizer.
    for path in (0, 1):
        _write_rf(transport, path, 0xEE, 0x4)
        _write_rf(transport, path, 0x33, 0x12)
        _write_rf(transport, path, 0x1A, 0x18)
        _write_rf(transport, path, 0xEE, 0x0)
        _write_rf(transport, path, 0x18, rf18)
    # rtw8822c_rstb_3wire(enable): commit the RF serial writes through the
    # analog parameter latch on both paths.
    _write_bb_mask(transport, 0x1830, 1 << 29, 1)
    _write_bb_mask(transport, 0x4130, 1 << 29, 1)
    # RxA enhance-Q is required by the vendor's 2.4 GHz receive path.
    _write_bb_mask(transport, 0x3C00 + (0xDF << 2), 1 << 18, int(is_2g))
    _write_bb_mask(transport, 0x1C90, 1 << 8, 1)
    _write_bb_mask(transport, 0x1830, 1 << 29, 1)
    _write_bb_mask(transport, 0x4130, 1 << 29, 1)

    if is_2g:
        # config_phydm_switch_bandwidth_8822c(..., CHANNEL_WIDTH_20).
        # The old 5 GHz path is hardware-proven; only 2.4 GHz needs this
        # additional CCK/RXBB programming to leave a prior 5 GHz state.
        _write_bb_mask(transport, 0x810, 0x3FF0, 0x19B)
        _write_bb_mask(transport, 0x9B0, 0xFFC0, 0)
        _write_bb_mask(transport, 0x9B0, 0xF, 0)
        _write_bb_mask(transport, 0xCBC, 1 << 21, 0)
        _write_bb_mask(transport, 0x1ABC, 1 << 30, 0)
        _write_bb_mask(transport, 0x1AE8, 1 << 31, 1)
        _write_bb_mask(transport, 0x1AEC, 0xF, 6)
        _write_bb_mask(transport, 0x88C, 0xF000, 1)
        rf18 |= (1 << 13) | (1 << 12)
        _write_bb_mask(transport, 0x1C90, 1 << 8, 0)
        for path in (0, 1):
            _write_rf(transport, path, 0xEE, 0x4)
            _write_rf(transport, path, 0x33, 0x12)
            _write_rf(transport, path, 0x3F, 0x18)
            _write_rf(transport, path, 0xEE, 0x0)
            _write_rf(transport, path, 0x18, rf18)
        _write_bb_mask(transport, 0x1C90, 1 << 8, 1)

    # AGC bank selectors.  Table indices are the 8822C enum values.
    if is_2g:
        cck, ofdm = 5, 6
    elif channel <= 64:
        cck, ofdm = 0, 1
    else:
        cck, ofdm = 0, 3
    _write_bb_mask(transport, 0x18AC, 0xF000, cck)
    _write_bb_mask(transport, 0x41AC, 0xF000, cck)
    _write_bb_mask(transport, 0x18AC, 0x1F0, ofdm)
    _write_bb_mask(transport, 0x41AC, 0x1F0, ofdm)
    _write_bb_mask(transport, 0x828, 0xF8, 0x0D)  # L_BND_DEFAULT_8822C

    if channel <= 10:
        sco = 0x9AA
    elif channel <= 12:
        sco = 0x96A
    elif channel <= 14:
        sco = 0x969
    elif channel <= 51:
        sco = 0x494
    else:
        sco = 0x412
    _write_bb_mask(transport, 0xC30, 0xFFF, sco)
    _write_bb_mask(transport, 0x808, 0x700000, 3 if channel == 11 else 1)
    _write_bb_mask(transport, 0x808, 0x70, 3 if (not is_2g or channel == 13) else 1)

    if is_2g:
        # config_phydm_switch_channel_8822c: make CCK decoding active.
        _write_bb_mask(transport, 0x1A9C, 1 << 20, 1)
        _write_bb_mask(transport, 0x1A14, 0x300, 0)
        transport.write8_clr(0x454, 1 << 7)
        _write_bb_mask(transport, 0x1A80, 1 << 18, 0)
        _write_bb_mask(transport, 0x1C80, 0x3F000000, 0xF)
        # rtw8822c_set_channel_bb: CCK TX filter coefficients are part of the
        # band/channel transition and must be refreshed after a 5 GHz hop.
        if channel == 14:
            _write_bb_mask(transport, 0x1A20, 0xFFFF0000, 0x3DA0)
            transport.write32(0x1A24, 0x4962C931)
            _write_bb_mask(transport, 0x1A28, 0x0000FFFF, 0x6AA3)
            _write_bb_mask(transport, 0x1A98, 0xFFFF0000, 0xAA7B)
            _write_bb_mask(transport, 0x1A9C, 0x0000FFFF, 0xF3D7)
            transport.write32(0x1AA0, 0x00000000)
            transport.write32(0x1AAC, 0xFF012455)
            transport.write32(0x1AB0, 0x0000FFFF)
        else:
            _write_bb_mask(transport, 0x1A20, 0xFFFF0000, 0x5284)
            transport.write32(0x1A24, 0x3E18FEC8)
            _write_bb_mask(transport, 0x1A28, 0x0000FFFF, 0x0A88)
            _write_bb_mask(transport, 0x1A98, 0xFFFF0000, 0xACC4)
            _write_bb_mask(transport, 0x1A9C, 0x0000FFFF, 0xC8B2)
            transport.write32(0x1AA0, 0x00FAF0DE)
            transport.write32(0x1AAC, 0x00122344)
            transport.write32(0x1AB0, 0x0FFFFFFF)
    else:
        _write_bb_mask(transport, 0x1A9C, 1 << 20, 0)
        _write_bb_mask(transport, 0x1A14, 0x300, 3)
        transport.write8_set(0x454, 1 << 7)
        _write_bb_mask(transport, 0x1A80, 1 << 18, 1)
        _write_bb_mask(transport, 0x1C80, 0x3F000000, 0x22)

    # rtw8822c_set_channel_bb(..., CHANNEL_WIDTH_20), required after every
    # band crossing and not only by the 2.4 GHz CCK branch.
    _write_bb_mask(transport, 0x810, 0x3FF0, 0x19B)
    _write_bb_mask(transport, 0x9B0, 0xFFC0, 0)
    _write_bb_mask(transport, 0x9B0, 0xF, 0)
    _write_bb_mask(transport, 0x9B4, 0x700, 7)
    _write_bb_mask(transport, 0x9B4, 0x700000, 6)
    _write_bb_mask(transport, 0x1ABC, 1 << 30, 0)
    _write_bb_mask(transport, 0x88C, 0xF000, 1)
    _write_bb_mask(transport, 0xCBC, 1 << 21, 0)

    _set_channel_mac(transport, channel)

    # Upstream order is BB -> MAC -> RF -> IGI.  Re-issue the RF latch at the
    # tail because monitor-mode setup can touch the BB 3-wire gate.
    _write_bb_mask(transport, 0x1C90, 1 << 8, 0)
    for path in (0, 1):
        _write_rf(transport, path, 0xEE, 0x4)
        _write_rf(transport, path, 0x33, 0x12)
        _write_rf(transport, path, 0x1A, 0x18)
        _write_rf(transport, path, 0xEE, 0x0)
        _write_rf(transport, path, 0x18, rf18)
    _write_bb_mask(transport, 0x1C90, 1 << 8, 1)
    _write_bb_mask(transport, 0x1830, 1 << 29, 1)
    _write_bb_mask(transport, 0x4130, 1 << 29, 1)
    _apply_phy_changes(transport)
