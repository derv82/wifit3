"""Read-only state probe for a (possibly wedged) RT3070 — NO bring-up, NO TX.

Constructs the transport around the live device and reads key registers directly to
assess: is the chip alive on USB? is RX/TX enabled? is WPDMA running or stalled? is the
RX bulk-IN pipe producing? Pure control-transfer reads + one short bulk-IN read — safe
(no 802.11 TX, no re-init). Close any other wifit3 instance first (it holds the handle).

    uv run python scripts/rt3070/probe_state.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rt3070 import constants as C
from wifit3.chips.rt3070.transport import RT3070Transport


def _r(t, addr, name):
    try:
        v = t.register_read(addr)
        return v, f"0x{addr:04x} {name:16} = 0x{v:08x}"
    except usb.core.USBError as e:
        return None, f"0x{addr:04x} {name:16} = ERROR {e}"


def main() -> int:
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=0x148F, idProduct=0x3070, backend=backend)
    if dev is None:
        print("[FAIL] no 148f:3070 on the bus (plug in + WinUSB-bind)")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        print(f"[note] set_configuration: {e}")

    # short per-read timeout so a wedged chip doesn't hang the probe
    t = RT3070Transport(dev, timeout_ms=300)

    print("=== direct register reads (control xfer) ===")
    csr0, line = _r(t, C.MAC_CSR0, "MAC_CSR0")
    print(line)
    if csr0 is not None:
        print(f"     -> silicon=0x{(csr0 >> 16) & 0xFFFF:04x} rev=0x{csr0 & 0xFFFF:04x}"
              f"  {'(alive on USB)' if csr0 not in (0, 0xFFFFFFFF) else '(BOGUS — pipe wedged?)'}")

    sysctrl, line = _r(t, C.MAC_SYS_CTRL, "MAC_SYS_CTRL")
    print(line)
    if sysctrl is not None:
        print(f"     -> ENABLE_TX={bool(sysctrl & 0x4)}  ENABLE_RX={bool(sysctrl & 0x8)}"
              f"  RESET_BBP={bool(sysctrl & 0x2)}  RESET_CSR={bool(sysctrl & 0x1)}")

    glo, line = _r(t, C.WPDMA_GLO_CFG, "WPDMA_GLO_CFG")
    print(line)
    if glo is not None:
        print(f"     -> TX_DMA_EN={bool(glo & 0x1)} TX_DMA_BUSY={bool(glo & 0x2)}"
              f"  RX_DMA_EN={bool(glo & 0x4)} RX_DMA_BUSY={bool(glo & 0x8)}")

    for addr, name in ((C.USB_DMA_CFG, "USB_DMA_CFG"), (C.RX_FILTER_CFG, "RX_FILTER_CFG"),
                       (C.PBF_SYS_CTRL, "PBF_SYS_CTRL"), (C.MAC_STATUS_CFG, "MAC_STATUS_CFG"),
                       (C.GPIO_CTRL, "GPIO_CTRL"), (C.INT_TIMER_CFG, "INT_TIMER_CFG")):
        print(_r(t, addr, name)[1])

    # BBP66 (RX gain / VGC) — indirect access; only if the chip answered above.
    if csr0 not in (None, 0, 0xFFFFFFFF):
        print("\n=== indirect reads (BBP) ===")
        try:
            bbp0 = t.bbp_read(0)
            bbp66 = t.bbp_read(66)
            print(f"BBP0 (id) = 0x{bbp0:02x}   BBP66 (RX VGC/gain) = 0x{bbp66:02x}"
                  f"  ({'default 0x1c' if bbp66 == 0x1c else 'NON-default'})")
        except usb.core.USBError as e:
            print(f"BBP read ERROR: {e}")

    # Is the RX bulk-IN pipe producing anything?
    print("\n=== RX bulk-IN probe (one short read on EP 0x81) ===")
    try:
        data = dev.read(0x81, 4096, 300)
        print(f"bulk-IN returned {len(data)} bytes (RX DMA IS producing)")
    except usb.core.USBError as e:
        errno = getattr(e, "errno", None)
        code = getattr(e, "backend_error_code", None)
        print(f"bulk-IN: {e}  (errno={errno} libusb={code})  "
              f"{'— timeout: pipe alive but no RX data' if (code == -7 or errno == 110) else '— pipe error'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
