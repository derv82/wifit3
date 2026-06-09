"""TEMPORARY placeholder driver for the Ralink RT3070 (ALFA AWUS036NH).

Exists ONLY so the manager recognizes ``148f:3070`` and the splash can surface the card
as *present-but-unbound* for Tier-0 device-setup UI testing [DEVICE-SETUP.md]. The RT3070
bring-up isn't ported yet — so ``from_usb_device`` deliberately refuses construction. While
the card is unbound the manager classifies it before ever calling that, so this never fires
in the UI test.

**This file gets REPLACED by the real clean-room ``RT3070Driver``** — the lead-approved plan
is a standalone ``chips/rt3070/`` port (NOT a ``chips/rt2800usb/`` DeviceID delta; that
shared base is proven non-byte-perfect). Full handoff brief, verified card facts, and the
``verify_pcap rt3070`` gate: ``chips/rt3070/RT3070.md``.
"""
from __future__ import annotations

from typing import ClassVar, List

import usb.core

from wifit3.engine.protocols import DeviceID

_VID_RALINK = 0x148F
_PID_RT3070 = 0x3070


class RT3070PlaceholderDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(_VID_RALINK, _PID_RT3070, "Ralink RT3070 / ALFA AWUS036NH"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))  # 2.4 GHz, 1T1R

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID):
        raise NotImplementedError(
            "RT3070 (AWUS036NH) bring-up is not ported yet — this is a Tier-0 "
            "device-setup UI placeholder only.")
