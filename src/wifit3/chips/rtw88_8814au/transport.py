"""RTL8814AU transport — alias over the shared rtw88 transport.

The USB control-transfer protocol (bRequest=0x05 vendor xfer) is identical
across all rtw88 USB chips. Kept as a thin shim so the chip-specific modules
can type-hint their own class name.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.transport import Rtw88Transport


class RTL8814AUTransport(Rtw88Transport):
    """Type-named alias used by the 8814au modules."""
