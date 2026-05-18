"""RTL8821AU transport — thin alias over the shared rtw88 transport.

The USB control-transfer protocol is identical across all rtw88 USB chips
(8821a, 8812a, 8822b, 8822c, ...). All chip-specific logic lives in
:class:`~wifit3.chips.rtw88_base.transport.Rtw88Transport`.

Kept here as an import shim so existing module-qualified type hints
(`from .transport import RTL8821AUTransport`) continue to work.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.transport import (
    USB_CMD_REQ,
    USB_REQTYPE_READ,
    USB_REQTYPE_WRITE,
    USB_VENQT_CMD_IDX,
    Rtw88Transport,
)

__all__ = [
    "RTL8821AUTransport",
    "USB_CMD_REQ",
    "USB_REQTYPE_READ",
    "USB_REQTYPE_WRITE",
    "USB_VENQT_CMD_IDX",
]


class RTL8821AUTransport(Rtw88Transport):
    """Backward-compat alias used by the 8821au modules + tests."""
