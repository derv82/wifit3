"""TEMPORARY placeholder driver for the Ralink RT3070 (ALFA AWUS036NH).

Exists ONLY so the manager recognizes ``148f:3070`` and the splash can surface the card
as *present-but-unbound* for Tier-0 device-setup UI testing [DEVICE-SETUP.md]. The RT3070
is rt2x00-family (the real home is ``rt2800usb``), but its RF/BBP bring-up isn't ported
yet — so ``from_usb_device`` deliberately refuses construction. While the card is unbound
the manager classifies it before ever calling that, so this never fires in the UI test.

Delete this dir (and its registration in ``wlan/manager.py``) once the RT3070 is either
brought up properly under ``rt2800usb`` or dropped.
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
