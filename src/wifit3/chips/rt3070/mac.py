"""MAC-level bring-up for the RT3070: chip probe, register init, radio enable.

Ported from ``rt2800lib.c`` (the chip logic) and ``rt2800usb.c`` (the USB glue).
Functions are added milestone by milestone; each is confirmed against the wire by
``scripts/verify_pcap.py rt3070`` before the next is started.
"""
from __future__ import annotations

from . import constants as C
from .transport import RT3070Transport


def probe_rt(t: RT3070Transport) -> tuple[int, int]:
    """Read the chip id + revision from MAC_CSR0 [SRC rt2800lib.c:11987-12031
    rt2800_probe_rt]. The AWUS036NH reports 0x3070 / 0x0201 (REV_RT3070F)."""
    reg = t.register_read(C.MAC_CSR0)
    rt = C.get_field(reg, C.MAC_CSR0_CHIPSET)
    rev = C.get_field(reg, C.MAC_CSR0_REVISION)
    return rt, rev


def probe_hw_gpio(t: RT3070Transport) -> None:
    """Set the rfkill-switch GPIO direction to input [SRC rt2800lib.c:12053-12059,
    inside rt2800_probe_hw]. The only register op probe_hw emits after the EFUSE
    dump; the rest of probe_hw / probe_hw_mode is in-memory spec setup."""
    reg = t.register_read(C.GPIO_CTRL)
    reg = C.set_field(reg, C.GPIO_CTRL_DIR2, 1)
    t.register_write(C.GPIO_CTRL, reg)
