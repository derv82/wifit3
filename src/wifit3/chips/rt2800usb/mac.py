"""rt2800usb MAC-layer helpers: chip probe + warm detection + USB-side
bootstrap.

The big rt2800_init_registers (~600 lines of MAC config) lands in
M2b-2 — this module currently just covers the USB-side bootstrap step
that the kernel runs first as ``rt2800usb_init_registers``.
"""
from __future__ import annotations

import logging
import time

from dataclasses import dataclass

from .constants import (
    MAC_ADDR_DW0,
    MAC_ADDR_DW1,
    MAC_CSR0,
    MAC_CSR0_CHIPSET_MASK,
    MAC_CSR0_CHIPSET_SHIFT,
    MAC_CSR0_REVISION_MASK,
    MAC_SYS_CTRL,
    MAC_SYS_CTRL_RESET_BBP,
    MAC_SYS_CTRL_RESET_CSR,
    PBF_SYS_CTRL,
    PBF_SYS_CTRL_READY,
    REGISTER_TIMEOUT_MS,
    RT_NAMES,
    RT_SUPPORTED,
    USB_DEVICE_MODE,
    USB_MODE_RESET,
    USB_VENDOR_REQUEST_OUT,
)
from .transport import RT2800USBTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChipId:
    """Result of reading MAC_CSR0 + decoding the silicon ID."""
    raw: int                # full MAC_CSR0 word
    silicon_id: int         # [31:16] = chipset family (RT5390 / RT5592 / RT3572 / ...)
    revision: int           # [15:0]  = revision (e.g. 0x0223 = REV_RT5592C)
    name: str               # human-readable, e.g. "RT5390" or "0x4567"
    is_supported: bool


def read_chip_id(t: RT2800USBTransport) -> ChipId:
    """Read MAC_CSR0 and decode the kernel-style chipset + revision.

    Mirrors ``rt2800_probe_rt`` (rt2800lib.c:11987-12031).
    """
    reg = t.read32(MAC_CSR0)
    silicon = (reg & MAC_CSR0_CHIPSET_MASK) >> MAC_CSR0_CHIPSET_SHIFT
    revision = reg & MAC_CSR0_REVISION_MASK
    name = RT_NAMES.get(silicon, f"0x{silicon:04x}")
    return ChipId(
        raw=reg,
        silicon_id=silicon,
        revision=revision,
        name=name,
        is_supported=silicon in RT_SUPPORTED,
    )


def read_perm_mac(t: RT2800USBTransport) -> bytes:
    """Read 6 permanent MAC bytes from MAC_ADDR_DW0/DW1.

    Kernel pulls these from EEPROM at probe time and writes them into
    MAC_ADDR_DW0/DW1 — so on a *warm* chip you get the real MAC. On a
    truly cold chip these may be zeros until M2 runs init_registers.
    """
    dw0 = t.read32(MAC_ADDR_DW0)
    dw1 = t.read32(MAC_ADDR_DW1)
    return bytes((
        dw0 & 0xFF,
        (dw0 >> 8) & 0xFF,
        (dw0 >> 16) & 0xFF,
        (dw0 >> 24) & 0xFF,
        dw1 & 0xFF,
        (dw1 >> 8) & 0xFF,
    ))


def is_chip_warm(t: RT2800USBTransport) -> bool:
    """True iff a prior session left the chip fully initialized.

    Heuristic verified [WIRE M1]: on a freshly-plugged dongle
    ``PBF_SYS_CTRL`` reads ``0x00002080`` — bit 13 (0x2000) is set as
    a "needs init" marker, bit 7 (READY) is also set. Kernel
    ``rt2800usb_init_registers`` (rt2800usb.c:280-281) explicitly
    clears bit 13 as part of init. So:

        cold = bit 13 set      (pre-init state)
        warm = bit 13 cleared + bit 7 set (post-init, FW running)
    """
    try:
        pbf_ctrl = t.read32(PBF_SYS_CTRL)
    except Exception as e:
        logger.debug("warm probe failed: %s", e)
        return False
    # The "pre-init" marker that init_registers clears.
    pre_init_bit = 1 << 13
    return bool(pbf_ctrl & PBF_SYS_CTRL_READY) and not (pbf_ctrl & pre_init_bit)


# ----------------------------------------------------------------------
# rt2800usb_init_registers — first thing to run post-FW upload.
# Port of rt2800usb.c:270-294.
# ----------------------------------------------------------------------
PBF_SYS_CTRL_PRE_INIT = 1 << 13   # bit kernel clears in init_registers


def usb_init_registers(t: RT2800USBTransport) -> None:
    """USB-side bootstrap that runs after FW upload.

    Kernel sequence (rt2800usb.c:270-294):

        wait_csr_ready                          MAC_CSR0 non-zero
        PBF_SYS_CTRL &= ~0x00002000             clear pre-init bit 13
        MAC_SYS_CTRL = RESET_CSR | RESET_BBP    reset MAC + BBP cores
        vendor_request(USB_DEVICE_MODE, 0,      reset USB endpoints
                       USB_MODE_RESET)
        MAC_SYS_CTRL = 0                        release MAC reset

    After this returns, ``is_chip_warm`` should report True (PBF bit 13
    cleared, READY still set).  M2b-2 will follow up with the bulk
    rt2800_init_registers MAC configuration.
    """
    # Brief wait for MAC_CSR0 to be readable (already true post-FW boot,
    # but kernel is paranoid).
    for _ in range(100):
        reg = t.read32(MAC_CSR0)
        if reg and reg != 0xFFFFFFFF:
            break
        time.sleep(0.001)
    else:
        raise IOError("usb_init_registers: MAC_CSR0 never came up")

    # Clear pre-init bit 13.
    pbf = t.read32(PBF_SYS_CTRL)
    t.write32(PBF_SYS_CTRL, pbf & ~PBF_SYS_CTRL_PRE_INIT & 0xFFFFFFFF)

    # Reset MAC + BBP cores via MAC_SYS_CTRL.
    t.write32(MAC_SYS_CTRL, MAC_SYS_CTRL_RESET_CSR | MAC_SYS_CTRL_RESET_BBP)

    # USB endpoint reset (vendor request: bRequest=USB_DEVICE_MODE,
    # wValue=USB_MODE_RESET, wIndex=0).
    t.dev.ctrl_transfer(
        USB_VENDOR_REQUEST_OUT,
        USB_DEVICE_MODE,
        USB_MODE_RESET,
        0,
        b"",
        REGISTER_TIMEOUT_MS,
    )

    # Release MAC reset.
    t.write32(MAC_SYS_CTRL, 0)
