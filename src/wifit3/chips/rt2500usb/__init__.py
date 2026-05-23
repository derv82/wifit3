"""rt2500usb chipset driver (Ralink RT2570).

Userland PyUSB port of the Linux ``rt2500usb`` kernel module
(data_dumps/rt2x00-source-v6.18/rt2500usb.{c,h} + rt2x00usb.{c,h}).

The RT2570 is the *older* Ralink USB generation: 16-bit CSR registers,
no firmware blob, BBP/RF reached indirectly through PHY_CSR busy-poll
registers. See RT2500USB.md for the per-chip ground-truth doc.
"""
