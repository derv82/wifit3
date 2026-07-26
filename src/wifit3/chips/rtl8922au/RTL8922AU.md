# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail: ASUS USB-BE93 (`0b05:1d84`). Ported from
the rtw89 vendor source (morrownr rtw89 v7.2), standalone, no shared base.

## Status

Milestone 1: USB register-access transport and the `Driver` subclass. `connect()` claims the
vendor interface and reads the first chip register. Power-on, firmware download, init, channel
tune, and TX are not yet ported.

## Orientation

- `constants.py` USB `VENQT` request codes, the address split, the CMAC window, the first
  power-on/info registers.
- `transport.py` `rtw89_usb_vendorreq` and the read/write ops, ported from rtw89-7.2 usb.c.
- `driver.py` the `Driver` subclass: `SUPPORTED_IDS`, `connect()`.

## Register access

A register op is a vendor control transfer on endpoint 0, `bRequest = 0x05` (`RTW89_USB_VENQT`),
`bmRequestType = 0xC0` read / `0x40` write. The address splits across the setup packet as
`wValue = addr & 0xFFFF`, `wIndex = (addr >> 16) & 0xFF`. [SRC] usb.c:31-32.

CMAC-window reads (`0xC000..0xFFFF`) can return `0xDEADBEEF` until the CMAC clock is enabled;
`read_cmac` re-enables it and re-reads. [SRC] usb.c:83-108.

## Capture

Cold-boot bundle: `usb_dumps_new2/captures_rtw89_8922au_git/` (capture-1/2/3). Taken on a
USB-2 path (the SuperSpeed check in `rtw89_usb_switch_mode` early-returns, so `switch_mode_be`
reads `R_BE_PAD_CTRL2` and the pcap opens with that read).

## Log

- 2026-07-26: milestone 1. Transport read32(R_BE_PAD_CTRL2) matches the pcap's first vendor
  op byte-for-byte. Next: `rtw89_usb_switch_mode_be` write path, then power-on.
