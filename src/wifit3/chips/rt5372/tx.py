"""TX descriptor build + bulk-OUT for the RT5372 (RT5392).

#TODO M6: port the TXINFO/TXWI descriptor build + bulk-OUT submit (family-common with
RT3070 — TXINFO 4B + TXWI 16B + frame + 4-byte align pad + 4-byte USB end pad, QSEL=2,
WIV=1). Wire-format byte-diffed against the kernel; the live inject is the user's
explicit action [[passive_by_default]], never fired by the driver.
"""
from __future__ import annotations

import usb.core


def send_frame(dev: usb.core.Device, ep: int, frame: bytes, *,
               use_no_ack: bool = True, timeout_ms: int = 1000) -> int:
    raise NotImplementedError("#TODO M6: rt5372 TX descriptor build + bulk-OUT")
