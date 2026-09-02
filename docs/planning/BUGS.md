# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## (CANTFIX) RTL8822BU TP-Link Archer T4U v3 / T4U+ ambiguity

**Problem.** TP-Link manufactured multiple different models that all share
the same exact device metadata.  A USB device with VID:PID `2357:0115` could be
Archer T4U v3, v3.2, or Archer T4U+. All other descriptors appear to be identical.

Known T4U+ sample (identical to T4U "v3.6" sample):
- `idVendor:idProduct`: `2357:0115`
- `manufacturer`: `Realtek`
- `product`: `802.11ac NIC`
- `serial`: `123456`
- `bcdDevice`: `0210`
- `bcdUSB/version`: `2.10`
- `speed`: `480`
- `chipset`: `RTL8822BU` (could be RTL8812BU given online reports)

Links:
- [linux-hardware.org@`2357:0115`](https://linux-hardware.org/?id=usb:2357-0115) (RTL8822BU, RTL8812BU)
- [Wi-Cat.ru@T4Uv3.2](https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3.2) *("probably rtl8812bu")*
  - [Wi-Cat.ru `2357:0115` disambiguation](https://shorturl.at/5iTC7)

**Direction.** Keep dumping device info from other models; spot the diff.

## Expand EFUSE support

Most drivers were ported against the single device they were tested on, so EFUSE derived values
(antenna count, TX power tables) sit in the code as constants. A device whose EFUSE differs then
gets wrong values with no error raised: little or no RX/TX, or wedging.

Chipsets confirmed to honor EFUSE: RTL8822CU.

Direction: per driver, list the EFUSE fields the vendor driver reads, compare against the wifit3
driver, port what is missing. The vendor's per field parsers are uniformly named, so the field list
comes out of the vendored source directly:

    grep -rhoE 'Hal_EfuseParse[A-Za-z0-9_]+' <vendor-tree>/ | sort -u

On a tree that compiles several chips that reports more fields than the one chip uses. Narrowing it
to the chip's own efuse reader gives the exact set, e.g. for RTL8822CU:

    sed -n '/rtl8822c_read_efuse/,/^}/p' hal/rtl8822c/rtl8822c_ops.c | grep -oE 'Hal_[A-Za-z0-9_]+' | sort -u
