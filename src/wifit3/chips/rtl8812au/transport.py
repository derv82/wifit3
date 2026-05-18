"""RTL8812AU transport — thin alias over the shared rtw88 transport.

The USB control-transfer protocol is identical across all rtw88 USB chips.
All transport logic lives in
:class:`wifit3.chips.rtw88_base.transport.Rtw88Transport`.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.transport import (
    USB_CMD_REQ,
    USB_REQTYPE_READ,
    USB_REQTYPE_WRITE,
    USB_VENQT_CMD_IDX,
    Rtw88Transport,
)


class RTL8812AUTransport(Rtw88Transport):
    """Backward-compat alias for the 8812au modules + tests."""


__all__ = [
    "RTL8812AUTransport",
    "USB_CMD_REQ",
    "USB_REQTYPE_READ",
    "USB_REQTYPE_WRITE",
    "USB_VENQT_CMD_IDX",
]
