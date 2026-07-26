"""RTL8922AU MAC helpers, ported from rtw89-7.2 (mac.c, core.c)."""

from .constants import (
    R_AX_SYS_CFG1, R_BE_SYS_CHIPINFO, R_AX_WLAN_XTAL_SI_CTRL,
    B_AX_CHIP_VER_MASK, B_BE_HW_ID_MASK,
    B_AX_WL_XTAL_SI_ADDR_MASK, B_AX_WL_XTAL_SI_DATA_MASK, B_AX_WL_XTAL_SI_MODE_MASK,
    B_AX_WL_XTAL_SI_CMD_POLL, XTAL_SI_NORMAL_READ, XTAL_SI_POLL_ATTEMPTS,
    XTAL_SI_CV, XTAL_SI_ACV_MASK, XTAL_SI_CHIP_ID_L, XTAL_SI_CHIP_ID_H,
)


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1      # trailing-zero count of the mask


def field_prep(mask: int, val: int) -> int:
    """FIELD_PREP: place `val` into `mask`'s field."""
    return (val << _shift(mask)) & mask


def field_get(mask: int, val: int) -> int:
    """u32_get_bits: extract `mask`'s field from `val`."""
    return (val & mask) >> _shift(mask)


def read_xtal_si(t, offset: int) -> int:
    """rtw89_mac_read_xtal_si_ax: indirect crystal-SI read. Writes an address+read command
    to XTAL_SI_CTRL, polls the same register until the command bit clears, returns the data
    byte. [SRC] mac.c:7208-7234."""
    cmd = (field_prep(B_AX_WL_XTAL_SI_ADDR_MASK, offset)
           | field_prep(B_AX_WL_XTAL_SI_MODE_MASK, XTAL_SI_NORMAL_READ)
           | B_AX_WL_XTAL_SI_CMD_POLL)
    t.write32(R_AX_WLAN_XTAL_SI_CTRL, cmd)
    for _ in range(XTAL_SI_POLL_ATTEMPTS):
        val32 = t.read32(R_AX_WLAN_XTAL_SI_CTRL)
        if not (val32 & B_AX_WL_XTAL_SI_CMD_POLL):
            return field_get(B_AX_WL_XTAL_SI_DATA_MASK, val32)
    return 0


def read_chip_ver(t) -> dict:
    """rtw89_read_chip_ver (BE path): read chip version, analog cut, hw id, and analog id.
    [SRC] core.c:7091-7130. The RTL8852A cv-fixup branch is not on the 8922A graph
    (its guard is chip_id == RTL8852A), so it is not ported here."""
    cv = field_get(B_AX_CHIP_VER_MASK, t.read32(R_AX_SYS_CFG1))
    acv = field_get(XTAL_SI_ACV_MASK, read_xtal_si(t, XTAL_SI_CV))
    cid = field_get(B_BE_HW_ID_MASK, t.read32(R_BE_SYS_CHIPINFO))
    aid = read_xtal_si(t, XTAL_SI_CHIP_ID_L) | (read_xtal_si(t, XTAL_SI_CHIP_ID_H) << 8)
    return {"cv": cv, "acv": acv, "cid": cid, "aid": aid}
