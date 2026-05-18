"""RTL8822BU transport — alias over the shared rtw88 transport.

The USB control-transfer protocol is identical across all rtw88 USB chips.
Kept as a thin shim so the chip-specific modules can type-hint their own
class name.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.transport import Rtw88Transport


class RTL8822BUTransport(Rtw88Transport):
    """Backward-compat alias used by the 8822bu modules."""
