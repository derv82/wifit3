"""RTL8821CU HALMAC power-sequence runtime + the 8821c transition tables.

The HALMAC power state machine moves the chip through CARDDIS -> CARDEMU -> ACT for
power-on (``card_en_flow``) and the reverse for power-off (``card_dis_flow``). Each
transition is a table of ``halmac_wlan_pwr_cfg`` rows; ``pwr_seq_parser_88xx`` walks the
flow's tables and ``pwr_sub_seq_parser_88xx`` runs each row whose interface- and cut-mask
match this card. WRITE is a read-modify-write; POLLING reads until the masked value
matches; DELAY and READ touch no register; END terminates a table.

The four 8821c tables are transcribed 1:1 from the vendor source (SDIO/PCI rows kept
verbatim — the interface filter drops them for USB, but a faithful copy is the safest
port). These differ from the 8822b tables (no 0xFF0A/0xFF0B/0x0012 LDO rows, different
PCI block, no cut-C 0x10A8 rows, the ACT table ends at 0x007C) — which is exactly why
this is a separate self-contained port, not a reuse.

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_pwr_seq_8821c.c:20-349  (the tables/flows)
  [SRC] hal/halmac/halmac_pwr_seq_cmd.h:21-96                              (cmd/base/intf/cut enums + struct)
"""
from __future__ import annotations

# command codes [SRC] halmac_pwr_seq_cmd.h:30-56
_CMD_READ = 0x00
_CMD_WRITE = 0x01
_CMD_POLLING = 0x02
_CMD_DELAY = 0x03
_CMD_END = 0x04

# base address block [SRC] :61-64 — the USB filter drops every SDIO row, so only
# ADDR_MAC ever executes here.
_ADDR_MAC = 0x00
_ADDR_USB = 0x01
_ADDR_PCIE = 0x02
_ADDR_SDIO = 0x03

# interface masks [SRC] :67-70
_S = 1 << 0          # SDIO
_U = 1 << 1          # USB
_P = 1 << 2          # PCI
_A = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)   # ALL

# cut masks [SRC] :73-81 — every 8821c card_en/dis row is CUT_ALL.
_CUT_ALL = 0xFF

# delay unit [SRC] :83-86
_DELAY_US = 0
_DELAY_MS = 1

INTF_USB = _U
POLLING_CNT = 20000  # HALMAC_PWR_POLLING_CNT [SRC] :21; replay matches on read #1


def _b(*bits: int) -> int:
    """BIT(a) | BIT(b) | ... — readability shim matching the source's BIT() rows."""
    m = 0
    for n in bits:
        m |= 1 << n
    return m


# Each row: (offset, cut_msk, intf_msk, base, cmd, msk, value)
# [SRC] halmac_pwr_seq_8821c.c:20-57 — transcribed verbatim.
_TRANS_CARDDIS_TO_CARDEMU = [
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, _b(0), 0),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_POLLING, _b(1), _b(1)),
    (0x004A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, _b(0), 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(3, 4, 7), 0),
    (0x0300, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x0301, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

# [SRC] halmac_pwr_seq_8821c.c:59-162 — transcribed verbatim.
_TRANS_CARDEMU_TO_ACT = [
    (0x0020, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0001, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_DELAY, 1, _DELAY_MS),
    (0x0000, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, _b(5), 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(4, 3, 2), 0),
    (0x0075, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, _b(1), _b(1)),
    (0x0075, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(0), 0),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(7), 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(4, 3), 0),
    (0x10C3, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, _b(0), 0),
    (0x0020, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(3), _b(3)),
    (0x0074, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(5), _b(5)),
    (0x0022, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0x0062, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(7, 6, 5), _b(7, 6, 5)),
    (0x0061, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(7, 6, 5), 0),
    (0x007C, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

# [SRC] halmac_pwr_seq_8821c.c:164-221 — transcribed verbatim.
_TRANS_ACT_TO_CARDEMU = [
    (0x0093, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0xC4),
    (0x001F, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x0049, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(0), _b(0)),
    (0x0002, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0x10C3, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, _b(0), 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(1), _b(1)),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, _b(1), 0),
    (0x0020, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(3), 0),
    (0x0000, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, _b(5), _b(5)),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

# [SRC] halmac_pwr_seq_8821c.c:223-335 — transcribed verbatim.
_TRANS_CARDEMU_TO_CARDDIS = [
    (0x0007, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, 0xFF, 0x20),
    (0x0067, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(5), 0),
    (0x0005, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, _b(2), _b(2)),
    (0x004A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, _b(0), 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(5), 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(4), 0),
    (0x004F, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(0), 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0x0046, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(6), _b(6)),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(2), 0),
    (0x0046, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(7), _b(7)),
    (0x0062, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, _b(4), _b(4)),
    (0x0081, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, _b(7, 6), 0),
    (0x0005, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, _b(3, 4), _b(3)),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, _b(0), _b(0)),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_POLLING, _b(1), 0),
    (0x0090, _CUT_ALL, _U | _P, _ADDR_MAC, _CMD_WRITE, _b(1), 0),
    (0x0044, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0),
    (0x0040, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x90),
    (0x0041, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x00),
    (0x0042, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x04),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

# Card enable / disable flows [SRC] halmac_pwr_seq_8821c.c:338-349.
CARD_EN_FLOW = [_TRANS_CARDDIS_TO_CARDEMU, _TRANS_CARDEMU_TO_ACT]
CARD_DIS_FLOW = [_TRANS_ACT_TO_CARDEMU, _TRANS_CARDEMU_TO_CARDDIS]


def _run_table(t, table, cut, intf) -> None:
    """pwr_sub_seq_parser_88xx [SRC] halmac_common_88xx.c:3051."""
    for offset, cut_msk, intf_msk, base, cmd, msk, value in table:
        if not ((intf_msk & intf) and (cut_msk & cut)):
            continue
        if cmd == _CMD_END:
            return
        if base == _ADDR_SDIO:
            # SDIO-local rows are filtered out for USB; reaching one means a bad port.
            raise NotImplementedError("RTL8821CU: SDIO-base pwr-seq row on a USB device")
        if cmd == _CMD_WRITE:
            v = t.read8(offset)
            v = (v & ~msk) | (value & msk)
            t.write8(offset, v & 0xFF)
        elif cmd == _CMD_POLLING:
            for _ in range(POLLING_CNT):
                if (t.read8(offset) & msk) == (value & msk):
                    break
            else:
                raise RuntimeError(f"RTL8821CU: pwr-seq polling 0x{offset:04x} timed out")
        elif cmd == _CMD_DELAY or cmd == _CMD_READ:
            pass            # DELAY: settle only (replay strips it); READ: no-op
        else:
            raise ValueError(f"RTL8821CU: bad pwr-seq cmd {cmd}")


def run_pwr_seq(t, flow, cut: int = _CUT_ALL, intf: int = _U) -> None:
    """pwr_seq_parser_88xx [SRC] halmac_common_88xx.c:2980 — walk a flow's tables.

    Every 8821c card_en/dis row is CUT_ALL, so the chip cut never filters here and
    the default (match-all) cut is faithful; the real cut is read at chip-id (a later
    milestone) and only matters for the init tables that follow power-on.
    """
    for table in flow:
        _run_table(t, table, cut, intf)
