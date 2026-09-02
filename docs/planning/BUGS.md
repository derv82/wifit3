# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

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
