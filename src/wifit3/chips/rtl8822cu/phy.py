"""RTL8822C BB/AGC/RF initialization tables and register protocol.

The four bundled binary tables are verbatim little-endian ``u32`` pairs
extracted from Realtek's GPL rtl88x2cu driver.  Their control records use the
same condition language as ``halhwimg8822c_{bb,rf}.c``; execution selects the
board's EFUSE RFE type before issuing any hardware writes.
"""
from __future__ import annotations

import logging
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    BB_PATH_A,
    BB_PATH_AB,
    BB_PATH_AUTO,
    BB_PATH_B,
    BB_PATH_NON,
    HAL_SPEC_RF_REG_PATH_NUM,
)
from .firmware import _fw_tx_desc, _set_le32_bits, _write_fw_packet
from .rx import parse_rx_pkt_desc, read_rx_burst
from .transport import RTL8822CUTransport
from .txpower import BAND_ON_2_4G, BAND_ON_5G
from .txpwr_index import (
    CHANNEL_WIDTH_20,
    RATE_SECTION_RATES,
    TxPwrIdxState,
    hal_com_get_txpwr_idx,
)
from .txpwr_tables import CCK, HT_1SS, HT_2SS, OFDM, VHT_1SS, VHT_2SS

logger = logging.getLogger(__name__)

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_TABLES = {
    "agc": _ASSET_DIR / "rtl8822c_agc.bin",
    "bb": _ASSET_DIR / "rtl8822c_bb.bin",
    "rf_a": _ASSET_DIR / "rtl8822c_rf_a.bin",
    "rf_b": _ASSET_DIR / "rtl8822c_rf_b.bin",
    "cal_init": _ASSET_DIR / "rtl8822c_cal_init.bin",
}

# FW-offload record encoding [SRC halmac_h2c_extra_info_nic.h:34-80, halmac_type.h:860]:
# a 12-byte record {word0, value, mask}; word0 = LEN(0x0C) | IO_CMD<<8 | MSK_EN<<15 | fields<<16.
_FWOFFLOAD_RECORDS_PER_PAGE = 337     # 337 * 12 = 4044 bytes fit one reserved page
_FWOFFLOAD_RSVD_BOUNDARY = 0x792


def _bb_w32_record(addr: int, value: int) -> bytes:
    w0 = 0x0C | (0xA << 8) | (1 << 15) | ((addr & 0xFFFF) << 16)
    return struct.pack("<III", w0, value & 0xFFFFFFFF, 0xFFFFFFFF)


def _rf_w_record(addr: int, value: int, path: int) -> bytes:
    w0 = 0x0C | (0x7 << 8) | (1 << 15) | ((addr & 0xFF) << 16) | ((path & 0xFF) << 24)
    return struct.pack("<III", w0, value & 0xFFFFFFFF, 0x000FFFFF)


def _delay_record(is_ms: bool, count: int) -> bytes:
    w0 = 0x0C | ((0x11 if is_ms else 0x10) << 8) | ((count & 0xFFFF) << 16)
    return struct.pack("<III", w0, 0, 0)

_PARA_IF = 0x8
_PARA_ELSE_IF = 0x9
_PARA_ELSE = 0xA
_PARA_END = 0xB
_PARA_CHECK = 0x4
_CUT_DONT_CARE = 0xF
_RFE_DONT_CARE = 0xFF
_RF_MASK = 0x000FFFFF        # RFREG_MASK [SRC phydm_types.h:414]
MASKDWORD = 0xFFFFFFFF       # [SRC phydm_types.h:403]


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
    """Load the board-selected AGC, BB, RF-A and RF-B initialization tables (MMIO path,
    runtime retune). The cold-boot path uses ``config_bb_rf`` (FW-offload) instead."""
    _phy_parameter_init(transport, post=False)
    _OFDM_AGC_L_BND.clear()
    _OFDM_AGC_L_BND.update(_detect_agc_lower_bound(cut, rfe_type))
    for address, value in selected_writes(load_table("agc"), cut=cut, rfe_type=rfe_type):
        _write_bb(transport, address, value)
    for address, value in selected_writes(load_table("bb"), cut=cut, rfe_type=rfe_type):
        _write_bb(transport, address, value)
    for path, name in ((0, "rf_a"), (1, "rf_b")):
        for address, value in selected_writes(load_table(name), cut=cut, rfe_type=rfe_type):
            _write_rf(transport, path, address, value)
    _phy_parameter_init(transport, post=True)


def _phy_param_init_block(transport: RTL8822CUTransport, *, post: bool,
                          dis_dpd_rate: int) -> None:
    """config_phydm_parameter_init_8822c one pass [SRC phydm_hal_api8822c.c:2206-2253]: the
    cck_gi_bound reads, the dis_dpd 0x0A70 write, the 3 wire enable, the CCK/OFDM block enable
    (0x1C3C = 0 PRE / 3 POST), then phydm_bb_reset. The IGI toggle is a runtime step, not here."""
    # phydm_cck_gi_bound_8822c [SRC phydm_hal_api8822c.c:2156-2173]: four odm_get_bb_reg reads.
    # The C stores cck_gi_u_bnd / cck_gi_l_bnd into physts software state that the monitor RX
    # path never consults, so only the register reads are reproduced.
    transport.read32(0x1A98)
    transport.read32(0x1AA8)
    transport.read32(0x1A98)
    transport.read32(0x1A70)
    # Disable low rate DPD: the two en_dis_dpd arms [SRC phydm_hal_api8822c.c:2222-2225] write
    # 0x3ff or 0x0 through phydm_set_dis_dpd_by_rate_8822c [SRC phydm_hal_api8822c.c:1616], which
    # is exactly what EfuseInfo.dis_dpd_rate resolves the EFUSE's txpwr_pg_mode to
    # [SRC ODM_CMNINFO_DIS_DPD rtl8822c_phy.c:455-456].
    set_bb_reg(transport, 0x0A70, 0x3FF, dis_dpd_rate)
    # Turn on 3 wire [SRC phydm_hal_api8822c.c:2229-2232].
    set_bb_reg(transport, 0x180C, 0x3, 3)
    set_bb_reg(transport, 0x180C, 1 << 28, 1)
    set_bb_reg(transport, 0x410C, 0x3, 3)
    set_bb_reg(transport, 0x410C, 1 << 28, 1)
    # PRE disables, POST enables the OFDM and CCK block [SRC phydm_hal_api8822c.c:2234-2245].
    set_bb_reg(transport, 0x1C3C, 0x3, 3 if post else 0)
    _bb_reset(transport)


def config_bb_rf(transport: RTL8822CUTransport, dev, bulk_out: int, bulk_in: int, *, cut: int,
                 rfe_type: int, crystal_cap: int, dis_dpd_rate: int) -> None:
    """Cycle-2 BB/RF config: config_phydm_parameter_init PRE, the FW-offload of the
    BB/AGC/cal_init/RF-A/RF-B parameter tables, then POST. Runs once at cold boot (the vendor
    path, not the MMIO ``initialize_phy``). ``bulk_in`` is the RX endpoint the per-batch
    CFG_PARAM_ACK interlock reads (see ``_wait_cfg_param_ack``)."""
    _phy_param_init_block(transport, post=False, dis_dpd_rate=dis_dpd_rate)
    _fw_offload_phy_tables(transport, dev, bulk_out, bulk_in, cut=cut, rfe_type=rfe_type,
                           crystal_cap=crystal_cap)
    _phy_param_init_block(transport, post=True, dis_dpd_rate=dis_dpd_rate)


# CFG_PARAM_ACK C2H interlock: the FW acknowledges every BB/RF FW-offload batch with an in-band
# C2H event on the RX bulk-IN. The vendor BLOCKS on each (wait_halmac_event) and verifies it
# before reusing the (full_fifo) reserved page for the next batch; skipping the wait pipelines
# the next dl_rsvd_page over a page the FW may still be executing, silently dropping BB/RF writes.
# C2H frame layout [SRC halmac_common_88xx.c:714-787 parse_c2h_pkt_88xx,
# halmac_fw_offload_c2h_nic.h:109-189]; the CFG_PARAM_ACK fields + verify
# [SRC halmac_common_88xx.c:1013-1067 get_h2c_ack_cfg_param_88xx].
_RXDESC_SZ = 24
_C2H_CMD_ID = 0xFF                  # C2H CMD_ID must be 0xFF, else "Not 0xFF cmd" (unhandled)
_C2H_SUB_H2C_ACK = 0x01            # C2H_SUB_CMD_ID: H2C_ACK header
_H2C_ACK_CMD_ID = 0xFF            # inner H2C_CMD_ID
_H2C_SUB_CFG_PARAM = 0x08          # inner H2C_SUB_CMD_ID: SUB_CMD_ID_CFG_PARAM
_H2C_RETURN_SUCCESS = 0x00         # HALMAC_H2C_RETURN_SUCCESS


def _parse_cfg_param_ack(buf: bytes) -> tuple[int, int, int, int] | None:
    """Walk the rx_pkt_desc-prefixed bulk-IN buffer for a CFG_PARAM_ACK C2H event, returning
    ``(h2c_seq, return_code, offset_accum, value_accum)`` for the first match, else None.
    Non-C2H frames and other C2H sub commands are skipped. [SRC halmac_common_88xx.c:714-787]"""
    pos = 0
    n = len(buf)
    while pos + _RXDESC_SZ <= n:
        try:
            stat = parse_rx_pkt_desc(buf, pos)
        except ValueError:
            return None
        if stat.total_size == 0 or pos + stat.total_size > n:
            return None
        if stat.is_c2h:
            c2h = buf[pos + stat.mpdu_offset: pos + stat.mpdu_offset + stat.pkt_len]
            if (len(c2h) >= 20 and c2h[0] == _C2H_CMD_ID and c2h[2] == _C2H_SUB_H2C_ACK
                    and c2h[5] == _H2C_ACK_CMD_ID
                    and (c2h[6] | (c2h[7] << 8)) == _H2C_SUB_CFG_PARAM):
                return (c2h[8] | (c2h[9] << 8), c2h[4],
                        int.from_bytes(c2h[12:16], "little"),
                        int.from_bytes(c2h[16:20], "little"))
        nxt = (pos + stat.total_size + 7) & ~7
        if nxt <= pos:
            return None
        pos = nxt
    return None


def _batch_accum(records: list[bytes]) -> tuple[int, int]:
    """The FW's CFG_PARAM_ACK offset/value accumulators for one batch: summed over the batch's
    records exactly as ``add_param_buf_88xx`` (BB/MAC write: +offset, +value; RF write:
    +(offset + (path << 8)), +value; delay/end contribute nothing). Reset per batch, since the
    vendor re-mallocs (and zeroes the accumulators) after each reserved-page send.
    [SRC halmac_common_88xx.c:1579-1630 add_param_buf_88xx, 1652-1683 malloc_cfg_param_buf_88xx]"""
    off = val = 0
    for rec in records:
        w0, value, _mask = struct.unpack("<III", rec)
        io_cmd = (w0 >> 8) & 0x7F
        if io_cmd in (0x04, 0x05, 0x06, 0x08, 0x09, 0x0A):      # MAC_W8/16/32, BB_W8/16/32
            off = (off + ((w0 >> 16) & 0xFFFF)) & 0xFFFFFFFF
            val = (val + value) & 0xFFFFFFFF
        elif io_cmd == 0x07:                                    # RF_W
            off = (off + ((w0 >> 16) & 0xFF) + (((w0 >> 24) & 0xFF) << 8)) & 0xFFFFFFFF
            val = (val + value) & 0xFFFFFFFF
    return off, val


def _wait_cfg_param_ack(dev, bulk_in: int, seq: int, offset_accum: int, value_accum: int,
                        *, timeout_ms: int = 100, tries: int = 5) -> None:
    """Block until the FW's CFG_PARAM_ACK C2H for batch ``seq`` arrives, mirroring the vendor
    ``wait_halmac_event(HALMAC_FEATURE_CFG_PARA)`` + ``get_h2c_ack_cfg_param_88xx``: match the
    echoed H2C seq, require return_code SUCCESS, and verify the FW offset/value accumulators
    equal the driver's. Raises on timeout (the vendor's -1 return) or on accumulator/return-code
    mismatch (HALMAC_CMD_PROCESS_ERROR). Synchronous single-threaded read: the async RX reader
    does not start until after monitor entry, so this owns the bulk-IN during bring-up.
    [SRC hal_halmac.c:5082-5105 rtw_halmac_cfg_phy_para, halmac_common_88xx.c:1013-1067]"""
    for _ in range(tries):
        buf = read_rx_burst(dev, bulk_in, timeout_ms=timeout_ms)
        if buf is None:
            continue
        ack = _parse_cfg_param_ack(buf)
        if ack is None:
            continue
        ack_seq, rc, off_acc, val_acc = ack
        if ack_seq != seq:
            # get_h2c_ack_cfg_param_88xx: seq mismatch -> log and ignore, keep waiting.
            logger.debug("rtl8822cu CFG_PARAM_ACK seq mismatch: want %d got %d", seq, ack_seq)
            continue
        if rc != _H2C_RETURN_SUCCESS:
            raise RuntimeError(f"CFG_PARAM seq {seq}: FW return code 0x{rc:02x} (not SUCCESS)")
        if off_acc != offset_accum or val_acc != value_accum:
            raise RuntimeError(
                f"CFG_PARAM seq {seq} accumulator mismatch: "
                f"FW off=0x{off_acc:08x} val=0x{val_acc:08x}, "
                f"driver off=0x{offset_accum:08x} val=0x{value_accum:08x}")
        logger.debug("rtl8822cu CFG_PARAM_ACK seq %d ok (off=0x%08x val=0x%08x)",
                     seq, off_acc, val_acc)
        return
    raise RuntimeError(
        f"CFG_PARAM seq {seq}: no FW ACK within {tries * timeout_ms} ms (C2H interlock timeout)")


def _fw_offload_apply(transport, dev, bulk_out: int, bulk_in: int, records: list[bytes],
                      seq: int) -> int:
    """Beacon-download one page of records, send the H2C cfg-param apply, then BLOCK on the FW's
    CFG_PARAM_ACK C2H for this batch (seq + offset/value accumulator verify) before releasing the
    next batch. Returns the next H2C sequence number. [SRC halmac_common_88xx.c:1421-1652,
    hal_halmac.c:5082-5105]"""
    _write_fw_packet(dev, transport, bulk_out, b"".join(records),
                     beacon=True, rsvd_boundary=_FWOFFLOAD_RSVD_BOUNDARY)
    pkt = bytearray(32)
    _set_le32_bits(pkt, 0x00, 0, 7, 0x01)               # CATEGORY
    _set_le32_bits(pkt, 0x00, 7, 1, 1)                  # ACK
    _set_le32_bits(pkt, 0x00, 8, 8, 0xFF)               # CMD_ID
    _set_le32_bits(pkt, 0x00, 16, 16, 0x08)             # SUB_CMD_ID = CFG_PARAM
    _set_le32_bits(pkt, 0x04, 0, 16, 12)                # TOTAL_LEN
    _set_le32_bits(pkt, 0x04, 16, 16, seq)              # SEQ_NUM
    _set_le32_bits(pkt, 0x08, 0, 16, len(records))      # NUM records
    _set_le32_bits(pkt, 0x08, 16, 1, 1)                 # INIT_CASE (full_fifo)
    dev.write(bulk_out, _fw_tx_desc(32, qsel=0x13, offset=0) + bytes(pkt), 1000)
    offset_accum, value_accum = _batch_accum(records)
    _wait_cfg_param_ack(dev, bulk_in, seq, offset_accum, value_accum)
    return seq + 1


def _fw_offload_flush(transport, dev, bulk_out: int, bulk_in: int, records: list[bytes],
                      seq: int) -> int:
    """Flush a table's records in 337-record reserved-page loads (the last is the END flush)."""
    for i in range(0, len(records), _FWOFFLOAD_RECORDS_PER_PAGE):
        seq = _fw_offload_apply(transport, dev, bulk_out, bulk_in,
                                records[i:i + _FWOFFLOAD_RECORDS_PER_PAGE], seq)
    return seq


def _fw_offload_phy_tables(transport: RTL8822CUTransport, dev, bulk_out: int, bulk_in: int, *,
                           cut: int, rfe_type: int, crystal_cap: int) -> None:
    """FW-offload the BB / AGC / cal_init / RF-A / RF-B parameter tables: each table is encoded
    into 12-byte records, reserved-page beacon-downloaded, then applied by an H2C cfg-param
    command. The H2C sequence continues from general_info (0, 1), so it starts at 2."""
    seq = 2
    bb = [_bb_w32_record(a, v) for a, v in selected_writes(load_table("bb"), cut=cut, rfe_type=rfe_type)]
    seq = _fw_offload_flush(transport, dev, bulk_out, bulk_in, bb, seq)
    _OFDM_AGC_L_BND.clear()
    _OFDM_AGC_L_BND.update(_detect_agc_lower_bound(cut, rfe_type))
    agc = [_bb_w32_record(a, v) for a, v in selected_writes(load_table("agc"), cut=cut, rfe_type=rfe_type)]
    seq = _fw_offload_flush(transport, dev, bulk_out, bulk_in, agc, seq)
    # phydm_set_crystal_cap_reg: pack the EFUSE crystal cap into 0x1040[23:17]=[16:10]
    # [SRC hal/phydm/phydm_cfotracking.c:213-218,313-318], called from _init_bb_reg after BB+AGC
    # [SRC hal/rtl8822c/rtl8822c_phy.c:176]. For 8822C: cap &= 0x7F; reg_val = cap | (cap << 7).
    cap = crystal_cap & 0x7F
    set_bb_reg(transport, 0x1040, 0x00FFFC00, cap | (cap << 7))
    # cal_init (RFK/DPK LUT): a 4-write MMIO prefix, then the whole table (no condition machine).
    transport.write32_set(0x1CD0, 1 << 28)
    transport.write32_set(0x1CD0, 1 << 29)
    transport.write32_set(0x1CD0, 1 << 30)
    transport.write32_clr(0x1CD0, 1 << 31)
    cal = [_bb_w32_record(a, v) for a, v in load_table("cal_init")]
    seq = _fw_offload_flush(transport, dev, bulk_out, bulk_in, cal, seq)
    for path, name in ((0, "rf_a"), (1, "rf_b")):
        recs: list[bytes] = []
        for addr, value in selected_writes(load_table(name), cut=cut, rfe_type=rfe_type):
            if addr == 0xFFE:
                recs.append(_delay_record(True, 50))
            elif addr == 0xFE:
                recs.append(_delay_record(False, 100))
            else:
                recs.append(_rf_w_record(addr, value, path))
                recs.append(_delay_record(False, 1))
        seq = _fw_offload_flush(transport, dev, bulk_out, bulk_in, recs, seq)


def set_bb_reg(transport: RTL8822CUTransport, address: int, mask: int, value: int) -> None:
    """odm_set_bb_reg. A full-width mask is a bare write; anything narrower reads first.
    odm_set_mac_reg resolves to this same function. [SRC rtl8822c_phy.c:613]"""
    if mask == MASKDWORD:
        transport.write32(address, value & 0xFFFFFFFF)
        return
    shift = (mask & -mask).bit_length() - 1
    current = transport.read32(address)
    transport.write32(address, (current & ~mask) | ((value << shift) & mask))


def get_bb_reg(transport: RTL8822CUTransport, address: int, mask: int = MASKDWORD) -> int:
    """odm_get_bb_reg. [SRC rtl8822c_phy.c:597]"""
    value = transport.read32(address)
    return (value & mask) >> ((mask & -mask).bit_length() - 1)


def get_rf_reg(transport: RTL8822CUTransport, path: int, address: int, mask: int = _RF_MASK) -> int:
    """config_phydm_read_rf_reg_8822c: read through the direct RF window (path A 0x3c00,
    path B 0x4c00). [SRC phydm_hal_api8822c.c:206]"""
    return get_bb_reg(transport, (0x3C00, 0x4C00)[path] + ((address & 0xFF) << 2),
                      mask & _RF_MASK)


def _check_bit_mask(bit_mask: int, data_original: int, data: int) -> int:
    """phydm_check_bit_mask_8822c. [SRC phydm_hal_api8822c.c:190]"""
    if bit_mask == _RF_MASK:
        return data
    return (data_original & ~bit_mask) | (data << ((bit_mask & -bit_mask).bit_length() - 1))


def set_rf_reg(transport: RTL8822CUTransport, path: int, address: int, mask: int,
               value: int) -> None:
    """config_phydm_write_rf_reg_8822c: RF 0x00 goes out as an address+data word through
    0x1808 / 0x4108; every other address is a masked write in the direct RF window.
    [SRC phydm_hal_api8822c.c:267]"""
    mask &= _RF_MASK
    if address != 0x00:
        set_bb_reg(transport, (0x3C00, 0x4C00)[path] + ((address & 0xFF) << 2), mask, value)
        time.sleep(0.000001)
        return
    if mask != _RF_MASK:
        value = _check_bit_mask(mask, get_rf_reg(transport, path, 0x00, _RF_MASK), value)
    set_bb_reg(transport, (0x1808, 0x4108)[path], MASKDWORD, (value & 0x000FFFFF) & 0x0FFFFFFF)


DBGPORT_PRI_2 = 2               # [SRC phydm_debug.h:318]


def set_bb_dbg_port(transport: RTL8822CUTransport, priority: int, port: int) -> None:
    """phydm_set_bb_dbg_port, JGR3 branch. Every 8822C user releases the port when done, so the
    priority screen always lets the write through. [SRC phydm_debug.c:132]"""
    set_bb_reg(transport, 0x1C3C, 0x000FFF00, port)


def get_bb_dbg_port_val(transport: RTL8822CUTransport) -> int:
    """phydm_get_bb_dbg_port_val, JGR3 branch. [SRC phydm_debug.c:168]"""
    return get_bb_reg(transport, 0x2DBC)


def _bb_reset(transport: RTL8822CUTransport) -> None:
    """phydm_bb_reset_8822c: toggle MAC 0x0 BIT(16) set/clear/set.
    [SRC phydm_hal_api8822c.c:124]"""
    transport.write32_set(0x0000, 1 << 16)
    transport.write32_clr(0x0000, 1 << 16)
    transport.write32_set(0x0000, 1 << 16)


def _igi_toggle(transport: RTL8822CUTransport) -> None:
    """phydm_igi_toggle_8822c: nudge IGI so BB HW emits a 3-wire command and RF HW enters RX
    mode (BB does not send one by itself when path/channel/BW changes).
    [SRC phydm_hal_api8822c.c:175]"""
    reg_1d70 = transport.read32(0x1D70)
    transport.write32(0x1D70, (reg_1d70 - 0x202) & 0xFFFFFFFF)
    transport.write32(0x1D70, reg_1d70)


@dataclass
class TrxPathState:
    """The phydm ``dm->{tx_2ss,tx_1ss,tx_ant,rx_ant}_status`` path bitmaps. Carried across calls:
    ``config_trx_mode`` reads ``tx_1ss_status`` back to resolve an AUTO 1SS path."""
    tx_2ss_status: int = BB_PATH_NON
    tx_1ss_status: int = BB_PATH_NON
    tx_ant_status: int = BB_PATH_NON
    rx_ant_status: int = BB_PATH_NON


def _set_rf_mode_table(transport: RTL8822CUTransport, tx_path_mode_table: int, rx_path: int) -> None:
    """phydm_set_rf_mode_table_8822c. Path-A can be neither shut down (it owns the synthesizer)
    nor put in standby (CCK sensitivity degrades at 1T1R-B). [SRC phydm_hal_api8822c.c:1120]"""
    if rx_path == BB_PATH_A:
        if tx_path_mode_table == BB_PATH_A:
            set_bb_reg(transport, 0x4100, 0xFFFFF, 0x0)
        else:
            set_bb_reg(transport, 0x4100, 0xFFFFF, 0x11112)
    else:
        set_bb_reg(transport, 0x4100, 0xFFFFF, 0x33312)


def _config_cck_tx_path(transport: RTL8822CUTransport, tx_path: int) -> None:
    """phydm_config_cck_tx_path_8822c. [SRC phydm_hal_api8822c.c:938]"""
    if tx_path == BB_PATH_A:
        set_bb_reg(transport, 0x1A04, 0xF0000000, 0x8)
    elif tx_path == BB_PATH_B:
        set_bb_reg(transport, 0x1A04, 0xF0000000, 0x4)
    else:
        set_bb_reg(transport, 0x1A04, 0xF0000000, 0xC)
    _bb_reset(transport)


def _config_cck_rx_path(transport: RTL8822CUTransport, rx_path: int) -> None:
    """phydm_config_cck_rx_path_8822c: CCK antenna mapping, Rx clk gating and the barker/CCA
    MRC enables. [SRC phydm_hal_api8822c.c:952]"""
    if rx_path == BB_PATH_A:
        set_bb_reg(transport, 0x1A04, 0x0F000000, 0x0)
        set_bb_reg(transport, 0x1A2C, 1 << 5, 0x0)
        set_bb_reg(transport, 0x1A2C, 0x00060000, 0x0)
        set_bb_reg(transport, 0x1A2C, 0x00600000, 0x0)
    elif rx_path == BB_PATH_B:
        set_bb_reg(transport, 0x1A04, 0x0F000000, 0x5)
        set_bb_reg(transport, 0x1A2C, 1 << 5, 0x1)
        set_bb_reg(transport, 0x1A2C, 0x00060000, 0x0)
        set_bb_reg(transport, 0x1A2C, 0x00600000, 0x1)
    elif rx_path == BB_PATH_AB:
        set_bb_reg(transport, 0x1A04, 0x0F000000, 0x1)
        set_bb_reg(transport, 0x1A2C, 1 << 5, 0x0)
        set_bb_reg(transport, 0x1A2C, 0x00060000, 0x1)
        set_bb_reg(transport, 0x1A2C, 0x00600000, 0x1)
    _bb_reset(transport)


def _config_ofdm_tx_path(transport: RTL8822CUTransport, tx_path_2ss: int,
                         tx_path_sel_1ss: int) -> None:
    """phydm_config_ofdm_tx_path_8822c. [SRC phydm_hal_api8822c.c:993]"""
    if tx_path_2ss != BB_PATH_AB:            # 1ss1T, do not config this with STBC
        if tx_path_sel_1ss == BB_PATH_A:
            set_bb_reg(transport, 0x0820, 0xFF, 0x1)
            set_bb_reg(transport, 0x1E2C, 0xFFFF, 0x0)
        else:
            set_bb_reg(transport, 0x0820, 0xFF, 0x2)
            set_bb_reg(transport, 0x1E2C, 0xFFFF, 0x0)
    elif tx_path_sel_1ss == BB_PATH_A:
        set_bb_reg(transport, 0x0820, 0xFF, 0x31)
        set_bb_reg(transport, 0x1E2C, 0xFFFF, 0x0400)
    elif tx_path_sel_1ss == BB_PATH_B:
        set_bb_reg(transport, 0x0820, 0xFF, 0x32)
        set_bb_reg(transport, 0x1E2C, 0xFFFF, 0x0400)
    else:
        set_bb_reg(transport, 0x0820, 0xFF, 0x33)
        set_bb_reg(transport, 0x1E2C, 0xFFFF, 0x0404)
    _bb_reset(transport)


def _config_ofdm_rx_path(transport: RTL8822CUTransport, rx_path: int) -> None:
    """phydm_config_ofdm_rx_path_8822c: MCS/NSS limit, antenna weighting, MRC eqz mode and the
    Rx ant / Rx CCA maps. Path-B alone is promoted to AB (non-MP). [SRC phydm_hal_api8822c.c:1027]"""
    ofdm_rx = rx_path
    if ofdm_rx == BB_PATH_B:
        ofdm_rx = BB_PATH_AB
        set_bb_reg(transport, 0x0CC0, 0x7FF, 0x0)
        set_bb_reg(transport, 0x0CC0, 1 << 22, 0x1)
        set_bb_reg(transport, 0x0CC8, 0x7FF, 0x0)
        set_bb_reg(transport, 0x0CC8, 1 << 22, 0x1)
    else:
        set_bb_reg(transport, 0x0CC0, 0x7FF, 0x400)
        set_bb_reg(transport, 0x0CC0, 1 << 22, 0x0)
        set_bb_reg(transport, 0x0CC8, 0x7FF, 0x400)
        set_bb_reg(transport, 0x0CC8, 1 << 22, 0x0)

    if ofdm_rx in (BB_PATH_A, BB_PATH_B):
        set_bb_reg(transport, 0x1D30, 0x300, 0x0)
        set_bb_reg(transport, 0x1D30, 0x600000, 0x0)
        set_bb_reg(transport, 0x0C44, 1 << 17, 0x0)
        set_bb_reg(transport, 0x0C54, 1 << 20, 0x0)
        set_bb_reg(transport, 0x0C38, 1 << 24, 0x0)
        set_bb_reg(transport, 0x0824, 0x000F0000, rx_path)
        set_bb_reg(transport, 0x0824, 0x0F000000, rx_path)
    elif ofdm_rx == BB_PATH_AB:
        set_bb_reg(transport, 0x1D30, 0x300, 0x1)
        set_bb_reg(transport, 0x1D30, 0x600000, 0x1)
        set_bb_reg(transport, 0x0C44, 1 << 17, 0x1)
        set_bb_reg(transport, 0x0C54, 1 << 20, 0x1)
        set_bb_reg(transport, 0x0C38, 1 << 24, 0x1)
        set_bb_reg(transport, 0x0824, 0x000F0000, BB_PATH_AB)
        set_bb_reg(transport, 0x0824, 0x0F000000, BB_PATH_AB)
    _bb_reset(transport)


def _rfe_8822c(transport: RTL8822CUTransport, rfe_type: int, path: int) -> None:
    """phydm_rfe_8822c: RFE pin mux, 2.4 GHz forces the no-path map.
    TODO: verify, untested here, needs an rfe_type 21/22 board. [SRC phydm_hal_api8822c.c:1146]"""
    rf_reg18 = get_rf_reg(transport, 0, 0x18)
    is_2g_ch = (rf_reg18 & 0xFF) <= 14
    if rfe_type in (21, 22):
        if is_2g_ch:
            path = BB_PATH_NON
        pins = {BB_PATH_NON: (0x7770, 0x7077), BB_PATH_A: (0x2300, 0x7077),
                BB_PATH_B: (0x7770, 0x2030), BB_PATH_AB: (0x2300, 0x2030)}.get(path)
        if pins:
            set_bb_reg(transport, 0x1840, 0xFFFF, pins[0])
            set_bb_reg(transport, 0x4144, 0xFFFF, pins[1])


def _config_tx_path(transport: RTL8822CUTransport, state: TrxPathState, tx_path_2ss: int,
                    tx_path_sel_1ss: int, tx_path_sel_cck: int) -> None:
    """phydm_config_tx_path_8822c. [SRC phydm_hal_api8822c.c:1083]"""
    state.tx_2ss_status = tx_path_2ss
    state.tx_1ss_status = tx_path_sel_1ss
    state.tx_ant_status = state.tx_2ss_status | state.tx_1ss_status
    _config_cck_tx_path(transport, tx_path_sel_cck)
    _config_ofdm_tx_path(transport, tx_path_2ss, tx_path_sel_1ss)
    _bb_reset(transport)


def _config_rx_path(transport: RTL8822CUTransport, state: TrxPathState, rx_path: int) -> None:
    """phydm_config_rx_path_8822c. [SRC phydm_hal_api8822c.c:1105]"""
    _config_cck_rx_path(transport, rx_path)
    _config_ofdm_rx_path(transport, rx_path)
    state.rx_ant_status = rx_path
    _bb_reset(transport)


def config_trx_mode(transport: RTL8822CUTransport, state: TrxPathState, *, tx_path_en: int,
                    rx_path: int, tx_path_sel_1ss: int, rfe_type: int) -> None:
    """config_phydm_trx_mode_8822c: the RF mode table, then the RX and TX antenna maps, the RFE
    pins and a closing IGI toggle. [SRC phydm_hal_api8822c.c:1197]"""
    tx_path_mode_table = tx_path_en
    tx_path_2ss = BB_PATH_AB
    disable_2sts_div_mode = False

    if rx_path & ~BB_PATH_AB:
        raise ValueError(f"RTL8822CU trx_mode: bad RX path 0x{rx_path:x}")
    if tx_path_en == BB_PATH_AUTO and tx_path_sel_1ss == BB_PATH_AUTO:
        disable_2sts_div_mode = True         # 2sts shut down, 1sts path-div still on
        tx_path_mode_table = BB_PATH_AB
    elif tx_path_en & ~BB_PATH_AB:
        raise ValueError(f"RTL8822CU trx_mode: bad TX path 0x{tx_path_en:x}")

    _set_rf_mode_table(transport, tx_path_mode_table, rx_path)
    _config_rx_path(transport, state, rx_path)

    if state.tx_1ss_status == BB_PATH_NON:
        state.tx_1ss_status = BB_PATH_A
    if tx_path_en in (BB_PATH_A, BB_PATH_B):
        tx_path_2ss = BB_PATH_NON
        tx_path_sel_1ss = tx_path_en
    elif tx_path_en == BB_PATH_AB:
        tx_path_2ss = BB_PATH_AB
        if tx_path_sel_1ss == BB_PATH_AUTO:
            tx_path_sel_1ss = state.tx_1ss_status
    elif disable_2sts_div_mode:
        tx_path_2ss = BB_PATH_NON
        tx_path_sel_1ss = state.tx_1ss_status
    _config_tx_path(transport, state, tx_path_2ss, tx_path_sel_1ss, tx_path_sel_1ss)

    if rfe_type in (21, 22):
        if state.tx_ant_status == BB_PATH_A and rx_path == BB_PATH_A:
            _rfe_8822c(transport, rfe_type, BB_PATH_A)
        elif state.tx_ant_status == BB_PATH_B and rx_path == BB_PATH_B:
            _rfe_8822c(transport, rfe_type, BB_PATH_B)
        else:
            _rfe_8822c(transport, rfe_type, BB_PATH_AB)

    _bb_reset(transport)
    _igi_toggle(transport)


def config_trx_path(transport: RTL8822CUTransport, state: TrxPathState, *, tx_path: int,
                    rx_path: int, max_tx_cnt: int, rfe_type: int) -> None:
    """rtw_phydm_config_trx_path: pick the 1SS TX path for the board's TX-path bitmap, then run
    the TRX-mode API. Path diversity and N-path TX are build options this port leaves off.
    [SRC hal/hal_dm.c:1568, 1484-1526]"""
    if tx_path == BB_PATH_AB:
        if max_tx_cnt == 2:
            tx_path_1ss = BB_PATH_A
        elif max_tx_cnt == 1:
            tx_path = tx_path_1ss = BB_PATH_A
        else:
            raise ValueError(f"RTL8822CU: invalid max_tx_cnt {max_tx_cnt}")
    else:
        tx_path_1ss = tx_path
    config_trx_mode(transport, state, tx_path_en=tx_path, rx_path=rx_path,
                    tx_path_sel_1ss=tx_path_1ss, rfe_type=rfe_type)


def _set_channel_mac(transport: RTL8822CUTransport, channel: int) -> None:
    """Mirror rtw_set_channel_mac for a fixed 20 MHz primary channel."""
    transport.write8(0x0483, 0x00)  # primary channel index 0, 20 MHz
    set_bb_reg(transport, 0x0668, (1 << 7) | (1 << 8), 0)
    set_bb_reg(transport, 0x0024, (1 << 20) | (1 << 21), 0)
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
    set_bb_reg(transport, 0x1D70, 0x7F, (igi - 2) & 0x7F)
    set_bb_reg(transport, 0x1D70, 0x7F00, (igi - 2) & 0x7F)
    set_bb_reg(transport, 0x1D70, 0x7F, igi)
    set_bb_reg(transport, 0x1D70, 0x7F00, igi)


def _phy_parameter_init(transport: RTL8822CUTransport, *, post: bool) -> None:
    """Port ``config_phydm_parameter_init_8822c`` for normal operation.

    The pre/post pair brackets the PHY tables.  Without the post phase the
    CCK and OFDM blocks remain disabled, so 2.4 GHz beacons never reach RXDMA.
    """
    set_bb_reg(transport, 0x180C, 0x3, 3)
    set_bb_reg(transport, 0x180C, 1 << 28, 1)
    set_bb_reg(transport, 0x410C, 0x3, 3)
    set_bb_reg(transport, 0x410C, 1 << 28, 1)
    set_bb_reg(transport, 0x1C3C, 0x3, 3 if post else 0)
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

    set_bb_reg(transport, 0x1C90, 1 << 8, 0)
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
    set_bb_reg(transport, 0x1830, 1 << 29, 1)
    set_bb_reg(transport, 0x4130, 1 << 29, 1)
    # RxA enhance-Q is required by the vendor's 2.4 GHz receive path.
    set_bb_reg(transport, 0x3C00 + (0xDF << 2), 1 << 18, int(is_2g))
    set_bb_reg(transport, 0x1C90, 1 << 8, 1)
    set_bb_reg(transport, 0x1830, 1 << 29, 1)
    set_bb_reg(transport, 0x4130, 1 << 29, 1)

    if is_2g:
        # config_phydm_switch_bandwidth_8822c(..., CHANNEL_WIDTH_20).
        # The old 5 GHz path is hardware-proven; only 2.4 GHz needs this
        # additional CCK/RXBB programming to leave a prior 5 GHz state.
        set_bb_reg(transport, 0x810, 0x3FF0, 0x19B)
        set_bb_reg(transport, 0x9B0, 0xFFC0, 0)
        set_bb_reg(transport, 0x9B0, 0xF, 0)
        set_bb_reg(transport, 0xCBC, 1 << 21, 0)
        set_bb_reg(transport, 0x1ABC, 1 << 30, 0)
        set_bb_reg(transport, 0x1AE8, 1 << 31, 1)
        set_bb_reg(transport, 0x1AEC, 0xF, 6)
        set_bb_reg(transport, 0x88C, 0xF000, 1)
        rf18 |= (1 << 13) | (1 << 12)
        set_bb_reg(transport, 0x1C90, 1 << 8, 0)
        for path in (0, 1):
            _write_rf(transport, path, 0xEE, 0x4)
            _write_rf(transport, path, 0x33, 0x12)
            _write_rf(transport, path, 0x3F, 0x18)
            _write_rf(transport, path, 0xEE, 0x0)
            _write_rf(transport, path, 0x18, rf18)
        set_bb_reg(transport, 0x1C90, 1 << 8, 1)

    # AGC bank selectors.  Table indices are the 8822C enum values.
    if is_2g:
        cck, ofdm = 5, 6
    elif channel <= 64:
        cck, ofdm = 0, 1
    else:
        cck, ofdm = 0, 3
    set_bb_reg(transport, 0x18AC, 0xF000, cck)
    set_bb_reg(transport, 0x41AC, 0xF000, cck)
    set_bb_reg(transport, 0x18AC, 0x1F0, ofdm)
    set_bb_reg(transport, 0x41AC, 0x1F0, ofdm)
    set_bb_reg(transport, 0x828, 0xF8, 0x0D)  # L_BND_DEFAULT_8822C

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
    set_bb_reg(transport, 0xC30, 0xFFF, sco)
    set_bb_reg(transport, 0x808, 0x700000, 3 if channel == 11 else 1)
    set_bb_reg(transport, 0x808, 0x70, 3 if (not is_2g or channel == 13) else 1)

    if is_2g:
        # config_phydm_switch_channel_8822c: make CCK decoding active.
        set_bb_reg(transport, 0x1A9C, 1 << 20, 1)
        set_bb_reg(transport, 0x1A14, 0x300, 0)
        transport.write8_clr(0x454, 1 << 7)
        set_bb_reg(transport, 0x1A80, 1 << 18, 0)
        set_bb_reg(transport, 0x1C80, 0x3F000000, 0xF)
        # rtw8822c_set_channel_bb: CCK TX filter coefficients are part of the
        # band/channel transition and must be refreshed after a 5 GHz hop.
        if channel == 14:
            set_bb_reg(transport, 0x1A20, 0xFFFF0000, 0x3DA0)
            transport.write32(0x1A24, 0x4962C931)
            set_bb_reg(transport, 0x1A28, 0x0000FFFF, 0x6AA3)
            set_bb_reg(transport, 0x1A98, 0xFFFF0000, 0xAA7B)
            set_bb_reg(transport, 0x1A9C, 0x0000FFFF, 0xF3D7)
            transport.write32(0x1AA0, 0x00000000)
            transport.write32(0x1AAC, 0xFF012455)
            transport.write32(0x1AB0, 0x0000FFFF)
        else:
            set_bb_reg(transport, 0x1A20, 0xFFFF0000, 0x5284)
            transport.write32(0x1A24, 0x3E18FEC8)
            set_bb_reg(transport, 0x1A28, 0x0000FFFF, 0x0A88)
            set_bb_reg(transport, 0x1A98, 0xFFFF0000, 0xACC4)
            set_bb_reg(transport, 0x1A9C, 0x0000FFFF, 0xC8B2)
            transport.write32(0x1AA0, 0x00FAF0DE)
            transport.write32(0x1AAC, 0x00122344)
            transport.write32(0x1AB0, 0x0FFFFFFF)
    else:
        set_bb_reg(transport, 0x1A9C, 1 << 20, 0)
        set_bb_reg(transport, 0x1A14, 0x300, 3)
        transport.write8_set(0x454, 1 << 7)
        set_bb_reg(transport, 0x1A80, 1 << 18, 1)
        set_bb_reg(transport, 0x1C80, 0x3F000000, 0x22)

    # rtw8822c_set_channel_bb(..., CHANNEL_WIDTH_20), required after every
    # band crossing and not only by the 2.4 GHz CCK branch.
    set_bb_reg(transport, 0x810, 0x3FF0, 0x19B)
    set_bb_reg(transport, 0x9B0, 0xFFC0, 0)
    set_bb_reg(transport, 0x9B0, 0xF, 0)
    set_bb_reg(transport, 0x9B4, 0x700, 7)
    set_bb_reg(transport, 0x9B4, 0x700000, 6)
    set_bb_reg(transport, 0x1ABC, 1 << 30, 0)
    set_bb_reg(transport, 0x88C, 0xF000, 1)
    set_bb_reg(transport, 0xCBC, 1 << 21, 0)

    _set_channel_mac(transport, channel)

    # Upstream order is BB -> MAC -> RF -> IGI.  Re-issue the RF latch at the
    # tail because monitor-mode setup can touch the BB 3-wire gate.
    set_bb_reg(transport, 0x1C90, 1 << 8, 0)
    for path in (0, 1):
        _write_rf(transport, path, 0xEE, 0x4)
        _write_rf(transport, path, 0x33, 0x12)
        _write_rf(transport, path, 0x1A, 0x18)
        _write_rf(transport, path, 0xEE, 0x0)
        _write_rf(transport, path, 0x18, rf18)
    set_bb_reg(transport, 0x1C90, 1 << 8, 1)
    set_bb_reg(transport, 0x1830, 1 << 29, 1)
    set_bb_reg(transport, 0x4130, 1 << 29, 1)
    _apply_phy_changes(transport)


def set_channel_fast(transport: RTL8822CUTransport, channel: int) -> None:
    """Fast scan hop: re-arm the CCK/OFDM RX blocks, then run the runtime switch.

    ``set_channel_20mhz`` alone wedges the 2.4 GHz RX chain under rapid hopping (only a
    full table replay revives it). Re-running the post PHY parameter init (a handful of
    BB writes that re-enable the RX blocks) before the switch keeps the device receiving
    at the scanner's cadence. Verified on the 2357:0137 adapter hardware.
    """
    _phy_parameter_init(transport, post=True)
    set_channel_20mhz(transport, channel)


_RXBB_MAX_GAIN_8822C = 0x14     # [SRC phydm_regconfig8822c.h:32]
_L_BND_DEFAULT_8822C = 0x0D     # L_BND_DEFAULT_8822C [SRC phydm_hal_api8822c.h:35]

# ofdm_rxagc_l_bnd[table]: the AGC lower bound for each table, filled from the RXAGC table load at
# bring up (_detect_agc_lower_bound). A table with no detected bound falls to _L_BND_DEFAULT_8822C.
_OFDM_AGC_L_BND: dict[int, int] = {}


def _detect_agc_lower_bound(cut: int, rfe_type: int) -> dict[int, int]:
    """phydm_agc_lower_bound_8822c: per table, the lower bound is the mp_gain field of that table's
    first R_0x1d90 row at RXBB_MAX_GAIN. [SRC phydm_regconfig8822c.c:98]"""
    l_bnd: dict[int, int] = {}
    for addr, data in selected_writes(load_table("agc"), cut=cut, rfe_type=rfe_type):
        if addr != 0x1D90:
            continue
        table = (data >> 22) & 0xF
        if table not in l_bnd and (data & 0x1F) == _RXBB_MAX_GAIN_8822C:
            l_bnd[table] = (data >> 16) & 0x3F
    return l_bnd


def _rstb_3wire(transport: RTL8822CUTransport, enable: bool) -> None:
    """phydm_rstb_3wire_8822c: force the BB HW to (re)issue the 3-wire RF command. Enable also
    latches anapar on both paths. [SRC phydm_hal_api8822c.c:94]"""
    if enable:
        set_bb_reg(transport, 0x1C90, 1 << 8, 1)
        set_bb_reg(transport, 0x1830, 1 << 29, 1)
        set_bb_reg(transport, 0x4130, 1 << 29, 1)
    else:
        set_bb_reg(transport, 0x1C90, 1 << 8, 0)


def _cck_agc_tab_sel(transport: RTL8822CUTransport, table: int) -> None:
    set_bb_reg(transport, 0x18AC, 0xF000, table)
    set_bb_reg(transport, 0x41AC, 0xF000, table)


def _ofdm_agc_tab_sel(transport: RTL8822CUTransport, table: int) -> None:
    set_bb_reg(transport, 0x18AC, 0x1F0, table)
    set_bb_reg(transport, 0x41AC, 0x1F0, table)
    set_bb_reg(transport, 0x828, 0xF8, _OFDM_AGC_L_BND.get(table, _L_BND_DEFAULT_8822C))


def _sco_trk_fc(transport: RTL8822CUTransport, channel: int) -> None:
    """phydm_sco_trk_fc_setting_8822c: clock-offset-tracking fc per channel. [SRC :1397]"""
    if channel in (13, 14):
        v = 0x969
    elif channel in (11, 12):
        v = 0x96A
    elif channel <= 10:
        v = 0x9AA
    elif channel <= 51:
        v = 0x494
    elif channel <= 55:
        v = 0x493
    elif channel <= 111:
        v = 0x453
    elif channel <= 119:
        v = 0x452
    elif channel <= 172:
        v = 0x412
    else:
        v = 0x411
    set_bb_reg(transport, 0xC30, 0xFFF, v)


def _tx_dfir(transport: RTL8822CUTransport, channel: int) -> None:
    """phydm_tx_dfir_setting_8822c. [SRC :1431]"""
    if channel <= 14:
        set_bb_reg(transport, 0x808, 0x700000, 0x3 if channel == 11 else 0x1)
        set_bb_reg(transport, 0x808, 0x70, 0x3 if channel == 13 else 0x1)
    else:
        set_bb_reg(transport, 0x808, 0x700000, 0x1)
        set_bb_reg(transport, 0x808, 0x70, 0x3)


def _switch_channel_mac(transport: RTL8822CUTransport, channel: int) -> None:
    """cfg_ch_bw_88xx: the MAC channel/bandwidth registers the driver writes between the two phydm
    switch calls (mac_switch_bandwidth, rtl8822c_phy.c:880/939). On our monitor path pri_ch_idx is
    HALMAC_CH_IDX_UNDEFINE(0) and bw is HALMAC_BW_20 (get_pri_ch_id returns 0 for CHANNEL_WIDTH_20).
    [SRC halmac_cfg_wmac_88xx.c:516-571, 581-621, 722-745, 531-550]"""
    # cfg_pri_ch_idx_88xx [:553]: REG_DATA_SC(0x0483) = BIT_TXSC_20M(txsc20) | BIT_TXSC_40M(txsc40)
    # [halmac_bit2.h:39716-39726]; txsc20 = pri_ch_idx, txsc40 = 9 for CH_IDX_1/CH_IDX_3 else 10.
    pri_ch_idx = 0                                       # HALMAC_CH_IDX_UNDEFINE
    txsc20 = pri_ch_idx
    txsc40 = 9 if pri_ch_idx in (1, 3) else 10           # HALMAC_CH_IDX_1 / HALMAC_CH_IDX_3
    transport.write8(0x0483, (txsc20 & 0xF) | ((txsc40 & 0xF) << 4))
    # cfg_bw_88xx [:582]: clear BIT7|BIT8 of REG_WMAC_TRXPTCL_CTL(0x0668); HALMAC_BW_20 ORs nothing
    # (BW_80 |= BIT8, BW_40 |= BIT7 are out of scope on our fixed 20 MHz path).
    trxptcl = transport.read32(0x0668) & ~((1 << 7) | (1 << 8))
    transport.write32(0x0668, trxptcl)
    # cfg_mac_clk_88xx [:722]: else/>=BW20 arm ORs MAC_CLK_HW_DEF_80M(0) << BIT_SHIFT_MAC_CLK_SEL(20)
    # into REG_AFE_CTRL1(0x0024) (BW_5/BW_10 narrowband arms out of scope); USTIME = MAC_CLK_SPEED(80).
    afe = transport.read32(0x0024) & ~((1 << 20) | (1 << 21))
    afe |= 0 << 20                                       # MAC_CLK_HW_DEF_80M << BIT_SHIFT_MAC_CLK_SEL
    transport.write32(0x0024, afe)
    transport.write8(0x055C, 80)                         # REG_USTIME_TSF = MAC_CLK_SPEED
    transport.write8(0x0638, 80)                         # REG_USTIME_EDCA = MAC_CLK_SPEED
    # cfg_ch_88xx [:531]: clear BIT7 of REG_CCK_CHECK(0x0454), set it only for ch > 35 (all 5G).
    cck_check = transport.read8(0x0454) & ~(1 << 7)
    if channel > 35:
        cck_check |= 1 << 7
    transport.write8(0x0454, cck_check)


def _switch_bandwidth(transport: RTL8822CUTransport, channel: int) -> None:
    """config_phydm_switch_bandwidth_8822c for 20 MHz: RX DFIR / clocks / CCK-PD BB writes, then
    the per-path RXBB RF program (rf_reg3f) and the RF18 re-latch. [SRC :1780]"""
    is_2g = channel <= 14
    rf18 = get_bb_reg(transport, 0x3C60, 0xFFFFF)   # config_phydm_read_rf_reg(A, RF_0x18)
    rf18 &= ~((1 << 13) | (1 << 12))
    set_bb_reg(transport, 0x810, 0x3FF0, 0x19B)     # RX DFIR (BW20)
    set_bb_reg(transport, 0x9B0, 0xFFC0, 0x0)       # small BW / pri ch
    set_bb_reg(transport, 0x9B4, 0x700, 0x7)        # DAC 480M
    set_bb_reg(transport, 0x9B4, 0x700000, 0x6)     # ADC 160M
    set_bb_reg(transport, 0x9B0, 0xF, 0x0)          # TX/RX RF BW
    rf18 |= (1 << 13) | (1 << 12)
    rf_reg3f = (1 << 4) | (1 << 3)                  # 0x18
    set_bb_reg(transport, 0xCBC, 1 << 21, 0x0)      # pilot smoothing on
    set_bb_reg(transport, 0x1ABC, 1 << 30, 0x0)     # CCK source 4
    set_bb_reg(transport, 0x1AE8, 1 << 31, 0x1)     # dynamic CCK PD th
    set_bb_reg(transport, 0x1AEC, 0xF, 0x6)
    set_bb_reg(transport, 0x88C, 0xF000, 0x1)       # subtune
    if is_2g:
        _cck_agc_tab_sel(transport, 5)              # CCK_BW20_8822C
        _ofdm_agc_tab_sel(transport, 6)             # OFDM_2G_BW20_8822C
    _rstb_3wire(transport, False)
    for win in (0x3C00, 0x4C00):                    # RXBB RF program, path A then B (WLANBB-1081)
        set_bb_reg(transport, win + (0xEE << 2), 0x4, 0x1)
        set_bb_reg(transport, win + (0x33 << 2), 0x1F, 0x12)
        set_bb_reg(transport, win + (0x3F << 2), 0xFFFFF, rf_reg3f)
        set_bb_reg(transport, win + (0xEE << 2), 0x4, 0x0)
    set_bb_reg(transport, 0x3C60, 0xFFFFF, rf18)    # odm_set_rf_reg(A, 0x18, rf_reg18)
    set_bb_reg(transport, 0x4C60, 0xFFFFF, rf18)    # odm_set_rf_reg(B, 0x18, rf_reg18)
    _rstb_3wire(transport, True)
    # phydm_bw_fixed_setting + bw_fixed_enable (8822C: R_0x878) [SRC phydm_api.c:823]
    set_bb_reg(transport, 0x878, 0xC0000000, 0x0)
    set_bb_reg(transport, 0x878, 1 << 28, 1)
    _bb_reset(transport)
    _igi_toggle(transport)


# config_phydm_set_txagc_to_hw_8822c: the per-hop TX-AGC flush that fires at the end of every
# channel switch (rtl8822c_phy.c:1067 set_tx_power_level -> set_txpwr_done). It writes 4 reference
# power indices (CCK/OFDM x path A/B) then a 12 entry power by rate diff table, all read back from
# the persistent dm->txagc_buff. [SRC phydm_hal_api8822c.c:476]
_CCK_REF = {0: 0x18A0, 1: 0x41A0}       # config_phydm_write_txagc_ref_8822c CCK ref [SRC :407]
_OFDM_REF = {0: 0x18E8, 1: 0x41E8}      # OFDM ref [SRC :415]
_CCK_REF_MASK, _OFDM_REF_MASK = 0x007F0000, 0x0001FC00

# ODM rate indices [SRC phydm_pre_define.h:229-294]; NUM_RATE_AC_2SS [SRC :330].
_ODM_RATE1M, _ODM_RATE11M, _ODM_RATE6M = 0x00, 0x03, 0x04
_ODM_RATEMCS7, _ODM_RATEMCS15 = 0x13, 0x1B
_ODM_VHTSS1MCS0, _ODM_VHTSS2MCS9 = 0x2C, 0x3F
_NUM_RATE_AC_2SS = 0x40

# The rate sections set_tx_power_level_by_path walks, in its order: CCK only when the band is
# 2.4 GHz, the four HT/VHT sections only when !under_survey_ch, which a monitor tune never is
# [SRC hal/rtl8822c/rtl8822c_phy.c:662-671].
_TXAGC_RATE_SECTIONS = (CCK, OFDM, HT_1SS, HT_2SS, VHT_1SS, VHT_2SS)


def _new_txagc_buff() -> list[list[int]]:
    """dm->txagc_buff[2][NUM_RATE_AC_2SS] initial state: buff[path][i] = i >> 2.
    [SRC phydm_hal_api8822c.c:72-73]"""
    return [[i >> 2 for i in range(_NUM_RATE_AC_2SS)] for _ in range(2)]


def _txagc_buff(transport: RTL8822CUTransport) -> list[list[int]]:
    """The device's persistent txagc_buff (mirrors dm->txagc_buff), carried across tunes so the CCK
    reference computed on a 2.4 GHz hop survives into the following 5 GHz hops."""
    buff = getattr(transport, "_txagc_buff_state", None)
    if buff is None:
        buff = _new_txagc_buff()
        transport._txagc_buff_state = buff
    return buff


def _fill_txagc_buff(buff: list[list[int]], channel: int, txpwr: TxPwrIdxState) -> None:
    """rtl8822c_set_tx_power_level: every RF register path, each running set_tx_power_level_by_path's
    rate sections into dm->txagc_buff. Unused paths are computed too, because the diff table phydm
    writes is a MIN over both [SRC hal/rtl8822c/rtl8822c_phy.c:675-687]. Each rate lands as
    hal_com_get_txpwr_idx's clamped 0..txgi_max index [SRC phydm_hal_api8822c.c:622-628]. On 5 GHz
    the CCK entries are left untouched, so buff[ODM_RATE11M] keeps its last 2.4 GHz value."""
    band = BAND_ON_2_4G if channel <= 14 else BAND_ON_5G
    for path in range(HAL_SPEC_RF_REG_PATH_NUM):
        for rs in _TXAGC_RATE_SECTIONS:
            if rs == CCK and band != BAND_ON_2_4G:      # [SRC hal/hal_com_phycfg.c:2343-2344]
                continue
            for rate_idx in RATE_SECTION_RATES[rs]:     # [SRC hal/hal_com_phycfg.c:2346-2358]
                # Monitor is 20 MHz, so cch, cch_20 and the operating channel are all `channel`.
                buff[path][rate_idx] = hal_com_get_txpwr_idx(
                    txpwr, path, rs, rate_idx, CHANNEL_WIDTH_20, band, channel, cch_20=channel)


def _bbrstb_txagc_off(transport: RTL8822CUTransport) -> None:
    """Clear 0x1C90 BIT(15) (bbrstb TX-AGC report) so the TX-AGC table can be written while
    bb_reset is low. Re-checked before each ref/diff write; only the first pass actually writes,
    matching the wire's one write to 0x1C90. [SRC phydm_hal_api8822c.c:401]"""
    if get_bb_reg(transport, 0x1C90, 0x8000):
        set_bb_reg(transport, 0x1C90, 0x8000, 0)


def _set_tx_power(transport: RTL8822CUTransport, channel: int,
                  txpwr: TxPwrIdxState | None) -> None:
    """rtl8822c_set_tx_power_level + set_txpwr_done: refill the persistent txagc_buff for this
    channel (the CCK section on 2.4 GHz only), then flush the 4 references and the MIN over paths
    diff table to hardware exactly as config_phydm_set_txagc_to_hw_8822c. The masked RMW on the ref
    registers keeps the thermal swing index in bits[6:0] the watchdog owns.
    [SRC rtl8822c_phy.c:675/689, phydm_hal_api8822c.c:476]

    A None ``txpwr`` is a TSSI mode part, and this driver then writes no TX power at all. That is a
    DELIBERATE DIVERGENCE from the vendor, which does write one: rtw_hal_dm_init skips only the PG
    loader hal_load_txpwr_info in that mode [SRC hal/hal_intf.c:199-201], while
    rtl8822c_set_tx_power_level and rtl8822c_set_txpwr_done still run on every tune
    [SRC hal/rtl8822c/rtl8822c_phy.c:675,689] and hal_com_get_txpwr_idx takes its base from the
    TSSI codeword instead [SRC hal/hal_com_phycfg.c:6321-6326]. That arm is not ported, so a TSSI
    8822CU gets RX with no TX power programmed, rather than a fabricated index."""
    if txpwr is None:
        return
    buff = _txagc_buff(transport)
    _fill_txagc_buff(buff, channel, txpwr)

    # ref_pow_cck/ofdm = txagc_buff[path][ODM_RATE11M / ODM_RATEMCS7]. [SRC phydm_hal_api8822c.c:481-484]
    cck_ref = (buff[0][_ODM_RATE11M], buff[1][_ODM_RATE11M])
    ofdm_ref = (buff[0][_ODM_RATEMCS7], buff[1][_ODM_RATEMCS7])
    for reg, mask, val in ((_CCK_REF[0], _CCK_REF_MASK, cck_ref[0]), (_CCK_REF[1], _CCK_REF_MASK, cck_ref[1]),
                           (_OFDM_REF[0], _OFDM_REF_MASK, ofdm_ref[0]), (_OFDM_REF[1], _OFDM_REF_MASK, ofdm_ref[1])):
        _bbrstb_txagc_off(transport)
        set_bb_reg(transport, reg, mask, val)

    # diff_tab[path][rate] = txagc_buff[path][rate] - ref (cck ref for CCK rates, ofdm ref for the
    # rest); diff_min = MIN over the two paths, 4 rates per dword to 0x3A00 + (rate & 0xfc).
    # [SRC phydm_hal_api8822c.c:508-580]
    diff = [[0] * _NUM_RATE_AC_2SS, [0] * _NUM_RATE_AC_2SS]
    for path in (0, 1):
        for rate in range(_ODM_RATE1M, _ODM_RATE11M + 1):
            diff[path][rate] = buff[path][rate] - cck_ref[path]
        for rate in range(_ODM_RATE6M, _ODM_RATEMCS15 + 1):
            diff[path][rate] = buff[path][rate] - ofdm_ref[path]
        for rate in range(_ODM_VHTSS1MCS0, _ODM_VHTSS2MCS9 + 1):
            diff[path][rate] = buff[path][rate] - ofdm_ref[path]
    for rate_range in (range(_ODM_RATE1M, _ODM_RATEMCS15 + 1),
                       range(_ODM_VHTSS1MCS0, _ODM_VHTSS2MCS9 + 1)):
        for rate in rate_range:
            if rate % 4 != 3:
                continue
            dword = 0
            for k in range(4):
                idx_min = min(diff[0][rate - 3 + k], diff[1][rate - 3 + k])
                dword |= (idx_min & 0x7F) << (8 * k)
            _bbrstb_txagc_off(transport)
            set_bb_reg(transport, 0x3A00 + (rate - 3), MASKDWORD, dword)


def switch_channel(transport: RTL8822CUTransport, channel: int,
                   txpwr: TxPwrIdxState | None) -> None:
    """Runtime channel switch: config_phydm_switch_channel_8822c + switch_bandwidth. Reads the
    live path-A RF18, recomputes the channel/sub-band fields, latches it on both paths through the
    3-wire reset, then reprograms the per-channel BB. [SRC phydm_hal_api8822c.c:1622]

    ``odm_set_rf_reg`` here is the DIRECT RF write: a masked RMW on the 0x3c00 (path A) / 0x4c00
    (path B) window, reg 0x18 -> +0x60. WIP: 20 MHz; the 2G CCK branch + switch_bandwidth land next.
    """
    is_2g = channel <= 14
    rf18 = get_bb_reg(transport, 0x3C60, 0xFFFFF)   # config_phydm_read_rf_reg(A, RF_0x18)
    rf18 &= ~0x703FF                                 # clear [18:17],[16],[9:8],[7:0]
    rf18 |= channel
    if not is_2g:
        rf18 |= (1 << 16) | (1 << 8)
        if channel > 144:
            rf18 |= 1 << 18
        elif channel >= 80:
            rf18 |= 1 << 17
    _rstb_3wire(transport, False)
    set_bb_reg(transport, 0x3C60, 0xFFFFF, rf18)     # odm_set_rf_reg(A, 0x18, rf18)
    set_bb_reg(transport, 0x4C60, 0xFFFFF, rf18)     # odm_set_rf_reg(B, 0x18, rf18)
    set_bb_reg(transport, 0x3F7C, 1 << 18, int(is_2g))  # odm_set_rf_reg(A, 0xdf, BIT18, is_2g)
    _rstb_3wire(transport, True)

    # AGC table selection (monitor is 20 MHz)
    if is_2g:
        _cck_agc_tab_sel(transport, 5)          # CCK_BW20_8822C
        _ofdm_agc_tab_sel(transport, 6)         # OFDM_2G_BW20_8822C
    elif channel <= 64:
        _ofdm_agc_tab_sel(transport, 1)         # OFDM_5G_LOW_BAND
    elif channel <= 144:
        _ofdm_agc_tab_sel(transport, 2)         # OFDM_5G_MID_BAND
    else:
        _ofdm_agc_tab_sel(transport, 3)         # OFDM_5G_HIGH_BAND
    _sco_trk_fc(transport, channel)
    _tx_dfir(transport, channel)

    if is_2g:
        # phydm_cck_tx_shaping_filter_8822c [SRC phydm_hal_api8822c.c:1326]
        if channel == 14:  # vendor ch14 branch [SRC phydm_hal_api8822c.c:1328]
            set_bb_reg(transport, 0x1A20, 0xFFFF0000, 0x3DA0)      # [SRC phydm_hal_api8822c.c:1330]
            set_bb_reg(transport, 0x1A24, 0xFFFFFFFF, 0x4962C931)  # [SRC phydm_hal_api8822c.c:1332]
            set_bb_reg(transport, 0x1A28, 0x0000FFFF, 0x6AA3)      # [SRC phydm_hal_api8822c.c:1334]
            set_bb_reg(transport, 0x1A98, 0xFFFF0000, 0xAA7B)      # [SRC phydm_hal_api8822c.c:1336]
            set_bb_reg(transport, 0x1A9C, 0x0000FFFF, 0xF3D7)      # [SRC phydm_hal_api8822c.c:1338]
            set_bb_reg(transport, 0x1AA0, 0xFFFFFFFF, 0x00000000)  # [SRC phydm_hal_api8822c.c:1340]
            set_bb_reg(transport, 0x1AAC, 0xFFFFFFFF, 0xFE012577)  # [SRC phydm_hal_api8822c.c:1342]
            set_bb_reg(transport, 0x1AB0, 0xFFFFFFFF, 0x0000FFFF)  # [SRC phydm_hal_api8822c.c:1344]
            set_bb_reg(transport, 0x818, 0xF8000000, 0x1F)        # [SRC phydm_hal_api8822c.c:1346]
        else:
            # phydm_cck_tx_shaping_filter_8822c (non-ch14) [SRC phydm_hal_api8822c.c:1347]
            set_bb_reg(transport, 0x1A20, 0xFFFF0000, 0x5284)
            set_bb_reg(transport, 0x1A24, 0xFFFFFFFF, 0x3E18FEC8)
            set_bb_reg(transport, 0x1A28, 0x0000FFFF, 0x0A88)
            set_bb_reg(transport, 0x1A98, 0xFFFF0000, 0xACC4)
            set_bb_reg(transport, 0x1A9C, 0x0000FFFF, 0xC8B2)
            set_bb_reg(transport, 0x1AA0, 0xFFFFFFFF, 0x00FAF0DE)
            set_bb_reg(transport, 0x1AAC, 0xFFFFFFFF, 0x00122344)
            set_bb_reg(transport, 0x1AB0, 0xFFFFFFFF, 0x0FFFFFFF)
            set_bb_reg(transport, 0x818, 0xF8000000, 0x18)
        # phydm_cck_rxiq(PHYDM_SET)
        set_bb_reg(transport, 0x1A9C, 1 << 20, 1)
        set_bb_reg(transport, 0x1A14, 0x300, 0)
        set_bb_reg(transport, 0x454, 1 << 7, 0)         # disable MAC CCK check
        set_bb_reg(transport, 0x1A80, 1 << 18, 0)       # disable BB CCK check
        set_bb_reg(transport, 0x1C80, 0x3F000000, 0xF)  # CCA mask default
    else:
        set_bb_reg(transport, 0x1A80, 1 << 18, 1)       # enable BB CCK check
        set_bb_reg(transport, 0x454, 1 << 7, 1)         # enable MAC CCK check
        set_bb_reg(transport, 0x1A9C, 1 << 20, 0)       # phydm_cck_rxiq(PHYDM_REVERT)
        set_bb_reg(transport, 0x1A14, 0x300, 3)
        set_bb_reg(transport, 0x1C80, 0x3F000000, 0x22)

    _bb_reset(transport)        # phydm_bb_reset_8822c
    _igi_toggle(transport)      # phydm_igi_toggle_8822c

    _switch_channel_mac(transport, channel)
    _switch_bandwidth(transport, channel)
    # rtl8822c_phy.c:1067: set_tx_power_level then set_txpwr_done flush the TX-AGC to hardware.
    _set_tx_power(transport, channel, txpwr)


def phy_bf_init(transport: RTL8822CUTransport) -> None:
    """rtl8822c_phy_bf_init: MU-MIMO retry limit and table state, the MU ack policy, the NDPA
    rate, and the STA2 CSI rate. Native access widths, not the 32-bit BB path.
    [SRC rtl8822c_phy.c:2051]"""
    mu_tx = transport.read32(0x14C0)
    mu_tx = (mu_tx | (1 << 16)) & ~(0xF << 12) | (0xA << 12)
    transport.write32(0x14C0, mu_tx & ~(1 << 7) & ~0x3F)
    transport.write8(0x167C, 0x70)          # ack policy 3, enabled
    transport.write16(0x1680, 0x0000)
    transport.write8(0x042F, transport.read8(0x042F) | 0x40)    # use NDPA parameter
    transport.write8(0x045F, 0x10)          # NDPA rate OFDM 6M, BW20
    transport.write8(0x06DF, (transport.read8(0x06DF) & 0xC0) | 0x04)
