"""RTL8822BU HALMAC power-sequence runtime + the 8822b transition tables.

The HALMAC power state machine moves the chip through CARDDIS -> CARDEMU -> ACT for
power-on (``card_en_flow``) and the reverse for power-off (``card_dis_flow``). Each
transition is a table of ``halmac_wlan_pwr_cfg`` rows; ``pwr_seq_parser_88xx`` walks the
flow's tables and ``pwr_sub_seq_parser_88xx`` runs each row whose interface- and cut-mask
match this card. WRITE is a read-modify-write; POLLING reads until the masked value
matches; DELAY and READ touch no register; END terminates a table.

The tables are transcribed 1:1 from the vendor (SDIO/PCI rows kept verbatim — the
interface filter drops them for USB, but a complete copy is the safest port). The 8822b
power-seq has no USB-specific divergence beyond that filter.

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_pwr_seq_8822b.c  (the tables)
  [SRC] hal/halmac/halmac_88xx/halmac_common_88xx.c:2980,3051,3099  (the runtime)
  [SRC] hal/halmac/halmac_pwr_seq_cmd.h:30-85                       (cmd / mask / base enums)
"""
from __future__ import annotations

# command codes [SRC] halmac_pwr_seq_cmd.h:30-56
_CMD_READ = 0x00
_CMD_WRITE = 0x01
_CMD_POLLING = 0x02
_CMD_DELAY = 0x03
_CMD_END = 0x04

# base [SRC] :61-64 — MAC reg vs SDIO-local (the USB filter drops every SDIO row, so
# only ADDR_MAC ever executes here).
_ADDR_MAC = 0x00
_ADDR_SDIO = 0x03

# interface masks [SRC] :67-70
_INTF_SDIO = 1 << 0
_INTF_USB = 1 << 1
_INTF_PCI = 1 << 2
_INTF_ALL = _INTF_SDIO | _INTF_USB | _INTF_PCI | (1 << 3)
_S = _INTF_SDIO
_U = _INTF_USB
_P = _INTF_PCI
_A = _INTF_ALL

# cut masks [SRC] :76-81 — HALMAC_PWR_CUT_x_MSK == BIT(chip_ver + 1); ALL == 0xFF.
_CUT_C = 1 << 3
_CUT_D = 1 << 4
_CUT_ALL = 0xFF

# delay unit [SRC] :84-85
_DELAY_US = 0
_DELAY_MS = 1

INTF_USB = _INTF_USB
POLLING_CNT = 5000  # HALMAC_PWR_POLLING_CNT order of magnitude; replay matches on read #1


def cut_mask(chip_ver: int) -> int:
    """HALMAC_CHIP_VER_x_CUT (A=0..F=5) -> HALMAC_PWR_CUT_x_MSK == BIT(chip_ver + 1).
    [SRC] halmac_common_88xx.c:2989-3014."""
    return 1 << (chip_ver + 1)


# Each row: (offset, cut_msk, intf_msk, base, cmd, msk, value)
# [SRC] halmac_pwr_seq_8822b.c:20-394 — transcribed verbatim.
_TRANS_CARDDIS_TO_CARDEMU = [
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 1 << 0, 0),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_POLLING, 1 << 1, 1 << 1),
    (0x004A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 1 << 0, 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, (1 << 3) | (1 << 4) | (1 << 7), 0),
    (0x0300, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x0301, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

_TRANS_CARDEMU_TO_ACT = [
    (0xFF0A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0xFF0B, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x0012, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 1, 0),
    (0x0012, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0020, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0001, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_DELAY, 1, _DELAY_MS),
    (0x0000, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, 1 << 5, 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, (1 << 4) | (1 << 3) | (1 << 2), 0),
    (0x0075, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, 1 << 1, 1 << 1),
    (0x0075, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 1 << 0, 0),
    (0xFF1A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 7, 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, (1 << 4) | (1 << 3), 0),
    (0x10C3, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, 1 << 0, 0),
    (0x0020, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 3, 1 << 3),
    (0x10A8, _CUT_C, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x10A9, _CUT_C, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0xEF),
    (0x10AA, _CUT_C, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0x0C),
    (0x0068, _CUT_C, _S, _ADDR_MAC, _CMD_WRITE, 1 << 4, 1 << 4),
    (0x0029, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0xF9),
    (0x0024, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 2, 0),
    (0x0074, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 1 << 5, 1 << 5),
    (0x00AF, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 5, 1 << 5),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

_TRANS_ACT_TO_CARDEMU = [
    (0x0003, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 2, 0),
    (0x0093, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0xC4),
    (0x001F, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0x00EF, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 0xFF, 0),
    (0xFF1A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 0xFF, 0x30),
    (0x0049, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 1, 0),
    (0x0006, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0002, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 1, 0),
    (0x10C3, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 1 << 0, 0),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 1, 1 << 1),
    (0x0005, _CUT_ALL, _A, _ADDR_MAC, _CMD_POLLING, 1 << 1, 0),
    (0x0020, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 3, 0),
    (0x0000, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, 1 << 5, 1 << 5),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

_TRANS_CARDEMU_TO_CARDDIS = [
    (0x0005, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 7, 1 << 7),
    (0x0007, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, 0xFF, 0x20),
    (0x0067, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, 1 << 5, 0),
    (0x0005, _CUT_ALL, _P, _ADDR_MAC, _CMD_WRITE, 1 << 2, 1 << 2),
    (0x004A, _CUT_ALL, _U, _ADDR_MAC, _CMD_WRITE, 1 << 0, 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 5, 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 4, 0),
    (0x004F, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 0, 0),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 1, 0),
    (0x0046, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 6, 1 << 6),
    (0x0067, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 2, 0),
    (0x0046, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 7, 1 << 7),
    (0x0062, _CUT_ALL, _S, _ADDR_MAC, _CMD_WRITE, 1 << 4, 1 << 4),
    (0x0081, _CUT_ALL, _A, _ADDR_MAC, _CMD_WRITE, (1 << 7) | (1 << 6), 0),
    (0x0005, _CUT_ALL, _U | _S, _ADDR_MAC, _CMD_WRITE, (1 << 3) | (1 << 4), 1 << 3),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 1 << 0, 1 << 0),
    (0x0086, _CUT_ALL, _S, _ADDR_SDIO, _CMD_POLLING, 1 << 1, 0),
    (0x0090, _CUT_ALL, _U | _P, _ADDR_MAC, _CMD_WRITE, 1 << 1, 0),
    (0x0044, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0),
    (0x0040, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x90),
    (0x0041, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x00),
    (0x0042, _CUT_ALL, _S, _ADDR_SDIO, _CMD_WRITE, 0xFF, 0x04),
    (0xFFFF, _CUT_ALL, _A, 0, _CMD_END, 0, 0),
]

# Card enable / disable flows [SRC] halmac_pwr_seq_8822b.c:397-408.
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
            raise NotImplementedError("RTL8822BU: SDIO-base pwr-seq row on a USB device")
        if cmd == _CMD_WRITE:
            v = t.read8(offset)
            v = (v & ~msk) | (value & msk)
            t.write8(offset, v & 0xFF)
        elif cmd == _CMD_POLLING:
            for _ in range(POLLING_CNT):
                if (t.read8(offset) & msk) == (value & msk):
                    break
            else:
                raise RuntimeError(f"RTL8822BU: pwr-seq polling 0x{offset:04x} timed out")
        elif cmd == _CMD_DELAY or cmd == _CMD_READ:
            pass            # DELAY: settle only (replay strips it); READ: no-op
        else:
            raise ValueError(f"RTL8822BU: bad pwr-seq cmd {cmd}")


def run_pwr_seq(t, flow, chip_ver: int, intf: int = _INTF_USB) -> None:
    """pwr_seq_parser_88xx [SRC] halmac_common_88xx.c:2980 — walk a flow's tables."""
    cut = cut_mask(chip_ver)
    for table in flow:
        _run_table(t, table, cut, intf)
