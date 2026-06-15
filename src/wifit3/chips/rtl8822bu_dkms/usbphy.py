"""RTL8822BU USB intf-PHY config — phy_cfg_usb_8822b.

Runs right after chip-ID detection and before read_chip_version. Applies the USB2
then USB3 intf-phy parameter tables for the chip's cut. On D-cut the USB2 table is
empty (terminator only) and the USB3 table has one matching entry, {0x0001, 0xA841},
emitted as three byte writes to the USB3 intf-phy window (0xFF0D/0E/0C).

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_phy_8822b.c:40-58  (param tables)
  [SRC] hal/halmac/halmac_88xx/halmac_common_88xx.c:3168             (parse_intf_phy_88xx)
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:107   (phy_cfg_usb_8822b)
  [SRC] hal/halmac/halmac_88xx/halmac_usb_88xx.c:475,529             (usbphy_write/page_switch)
"""
from __future__ import annotations

# intf-phy enums [SRC] halmac_type.h:22-46, 1246-1247
HAL_INTF_PHY_USB2 = 0
HAL_INTF_PHY_USB3 = 1
_PLATFORM_ALL = 0x0001          # HALMAC_INTF_PHY_PLATFORM_ALL = BIT(0) — what halmac_phy_cfg passes
_CUT_ALL = 0x7FFF              # HALMAC_INTF_PHY_CUT_ALL
_PLATFORM_FOR_ALL = 0x7FFF     # HALMAC_INTF_PHY_PLATFORM_FOR_ALL
_END = 0xFFFF                  # table terminator (offset)

# USB3 intf-phy access window (D-cut path) [SRC] halmac_usb_88xx.c:480-482
_REG_USB3_DATA_L = 0xFF0D
_REG_USB3_DATA_H = 0xFF0E
_REG_USB3_ADDR = 0xFF0C        # write strobe = BIT(7)

# {offset, value, cut_mask, platform_mask}; ip_sel is HALMAC_IP_INTF_PHY for every
# USB entry. [SRC] halmac_phy_8822b.c:40-58. CUT_D = BIT(4) = 0x10.
USB2_PHY_PARAM = [
    (_END, 0x0000, _CUT_ALL, _PLATFORM_FOR_ALL),          # terminator only
]
USB3_PHY_PARAM = [
    (0x0001, 0xA841, 0x0010, _PLATFORM_FOR_ALL),          # CUT_D
    (_END, 0x0000, _CUT_ALL, _PLATFORM_FOR_ALL),
]


def _cur_cut(chip_ver: int) -> int:
    """HALMAC_CHIP_VER_x_CUT (A=0..F=5) -> HALMAC_INTF_PHY_CUT_x (A=BIT(1)..F=BIT(6)).
    [SRC] halmac_common_88xx.c:3181-3205, halmac_type.h:22-29/563-572."""
    if 0 <= chip_ver <= 5:
        return 1 << (chip_ver + 1)
    raise ValueError(f"RTL8822BU: unsupported chip_ver 0x{chip_ver:02x}")


def _usb_page_switch(t, speed: int, page: int) -> None:
    """[SRC] halmac_usb_88xx.c:529 — USB3 returns immediately (no IO)."""
    if speed == HAL_INTF_PHY_USB3:
        return
    # TODO untestable: 8822b's USB2 intf-phy table is the terminator only, so the
    # USB2 page-select (USB_REG_PAGE / USB_PHY_PAGE0/1) is never reached on this card.
    raise NotImplementedError("RTL8822BU: USB2 intf-phy page switch unused")


def _usbphy_write(t, addr: int, data: int, speed: int) -> None:
    """[SRC] halmac_usb_88xx.c:475 usbphy_write_88xx."""
    if speed == HAL_INTF_PHY_USB3:
        t.write8(_REG_USB3_DATA_L, data & 0xFF)
        t.write8(_REG_USB3_DATA_H, (data >> 8) & 0xFF)
        t.write8(_REG_USB3_ADDR, addr | 0x80)             # BIT(7) = write strobe
    elif speed == HAL_INTF_PHY_USB2:
        t.write8(0xFE41, data & 0xFF)
        t.write8(0xFE40, addr)
        t.write8(0xFE42, 0x81)


def _parse_intf_phy(t, param, pltfm: int, intf_phy: int, chip_ver: int) -> None:
    """[SRC] halmac_common_88xx.c:3168 parse_intf_phy_88xx (USB branch)."""
    cur_cut = _cur_cut(chip_ver)
    for offset, value, cut, platform in param:
        if not ((cut & cur_cut) and (platform & pltfm)):
            continue
        if offset == _END:
            break
        _usb_page_switch(t, intf_phy, 1 if offset > 0x100 else 0)
        _usbphy_write(t, offset & 0xFF, value, intf_phy)


def phy_cfg_usb(t, chip_ver: int) -> None:
    """phy_cfg_usb_8822b: walk the USB2 then USB3 intf-phy param tables.
    [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:107."""
    _parse_intf_phy(t, USB2_PHY_PARAM, _PLATFORM_ALL, HAL_INTF_PHY_USB2, chip_ver)
    _parse_intf_phy(t, USB3_PHY_PARAM, _PLATFORM_ALL, HAL_INTF_PHY_USB3, chip_ver)
