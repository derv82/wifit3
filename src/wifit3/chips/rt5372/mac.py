"""MAC-level bring-up for the RT5372 (RT5392): chip probe, radio enable, register init.

Ported from ``rt2800lib.c`` (chip logic) + ``rt2800usb.c`` (USB glue), confirmed
against captures_rt2800usb_rt5372/capture-1 by ``scripts/verify_pcap.py rt5372``.

Scope note: the kernel's ``rt2800_init_registers`` is a giant per-chip switch. We
port the RT5390/RT5392 (RF53xx) path, marking the RT5390-only sibling writes
``#TODO untestable``. Unrelated chip families are out of scope — this driver claims
only 148f:5372 — so their switch arms are intentionally not transcribed.

M1 lands ``probe_rt`` + ``probe_hw_gpio`` (the bring-up's first wire ops, framing the
EFUSE dump); the radio-on / init_registers block lands in M2.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo, get_field, set_field
from .transport import RT5372Transport


def probe_rt(t: RT5372Transport) -> ChipInfo:
    """Read chip id + revision from MAC_CSR0 [SRC rt2800lib.c:11987-12031
    rt2800_probe_rt]. PAU05/PAU06 report rt=0x5392 (RT5392)."""
    reg = t.register_read(C.MAC_CSR0)
    return ChipInfo(rt=get_field(reg, C.MAC_CSR0_CHIPSET),
                    rev=get_field(reg, C.MAC_CSR0_REVISION))


def probe_hw_gpio(t: RT5372Transport) -> None:
    """rfkill-switch GPIO direction = input [SRC rt2800lib.c:12053-12059, inside
    rt2800_probe_hw]. The only register op probe_hw emits after the EFUSE dump."""
    reg = t.register_read(C.GPIO_CTRL)
    reg = set_field(reg, C.GPIO_CTRL_DIR2, 1)
    t.register_write(C.GPIO_CTRL, reg)
