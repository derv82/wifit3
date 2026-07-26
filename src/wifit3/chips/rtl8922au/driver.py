"""RTL8922AU driver: Realtek RTL8922A (802.11be) over USB, ported from rtw89-7.2.
See RTL8922AU.md.
"""
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from ..driver import Driver, DeviceID, ProgressCallback
from .constants import R_BE_PAD_CTRL2
from .transport import RTL8922AUTransport

logger = logging.getLogger(__name__)


class RTL8922AUDriver(Driver):
    """Realtek RTL8922A (802.11be) USB driver, ported from the rtw89 vendor source."""

    # The full rtw_8922au_id_table. [SRC] rtw8922au.c rtw_8922au_id_table. Retail brand/model
    # come from a runtime OUI read; only the card in hand (0b05:1d84) and the ASUS VID are named.
    SUPPORTED_IDS = [
        DeviceID(vid=0x0411, pid=0x03ef, chipset="RTL8922AU"),
        DeviceID(vid=0x0502, pid=0x76d7, chipset="RTL8922AU"),
        DeviceID(vid=0x056e, pid=0x4025, chipset="RTL8922AU"),
        DeviceID(vid=0x056e, pid=0x4026, chipset="RTL8922AU"),
        DeviceID(vid=0x057c, pid=0x8701, chipset="RTL8922AU"),
        DeviceID(vid=0x0b05, pid=0x1bcf, chipset="RTL8922AU", vendor="ASUS"),
        DeviceID(vid=0x0b05, pid=0x1bd2, chipset="RTL8922AU", vendor="ASUS"),
        DeviceID(vid=0x0b05, pid=0x1d84, chipset="RTL8922AU", vendor="ASUS", product_name="USB-BE93"),
        DeviceID(vid=0x0bda, pid=0x8912, chipset="RTL8922AU"),
        DeviceID(vid=0x0db0, pid=0xda0e, chipset="RTL8922AU"),
        DeviceID(vid=0x2001, pid=0x332b, chipset="RTL8922AU"),
        DeviceID(vid=0x2c4e, pid=0x0125, chipset="RTL8922AU"),
        DeviceID(vid=0x3625, pid=0x010a, chipset="RTL8922AU"),
        DeviceID(vid=0x37ad, pid=0x0100, chipset="RTL8922AU"),
        DeviceID(vid=0x37ad, pid=0x0101, chipset="RTL8922AU"),
        DeviceID(vid=0x7392, pid=0x3822, chipset="RTL8922AU"),
        DeviceID(vid=0x7392, pid=0x4822, chipset="RTL8922AU"),
        DeviceID(vid=0x7392, pid=0x5822, chipset="RTL8922AU"),
    ]
    # 2.4 GHz + 5 GHz at 20 MHz. TODO: verify + add the 6 GHz plan (8922a support_bands
    # includes 6 GHz). [SRC] rtw8922a.c:3210.
    SUPPORTED_CHANNELS = (
        list(range(1, 14))
        + [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
           132, 136, 140, 144, 149, 153, 157, 161, 165]
    )

    def __init__(self) -> None:
        super().__init__()
        self.dev: Optional[usb.core.Device] = None
        self.transport: Optional[RTL8922AUTransport] = None
        self._rx_cb: Optional[Callable] = None
        self._disconnect_cb: Optional[Callable[[], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8922AUDriver":
        self = cls()
        self.dev = dev
        self.transport = RTL8922AUTransport(dev)
        return self

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb

    def _claim_vendor_interface(self) -> Optional[int]:
        """Claim the vendor-specific (class 0xFF) interface that owns the bulk endpoints."""
        for intf in self.dev.get_active_configuration():
            if intf.bInterfaceClass != 0xFF:
                continue
            try:
                if self.dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    self.dev.detach_kernel_driver(intf.bInterfaceNumber)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(self.dev, intf.bInterfaceNumber)
            return intf.bInterfaceNumber
        return None

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Claim the vendor interface and read R_BE_PAD_CTRL2, the first register the rtw89
        USB probe reads (rtw89_usb_switch_mode_be). [SRC] usb.c:1143-1189."""
        iface = self._claim_vendor_interface()
        logger.info("RTL8922AU: claimed vendor interface %s", iface)
        pad_ctrl2 = self.transport.read32(R_BE_PAD_CTRL2)
        logger.info("RTL8922AU: R_BE_PAD_CTRL2 = 0x%08x", pad_ctrl2)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        if self.dev is not None:
            usb.util.dispose_resources(self.dev)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        raise NotImplementedError

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        raise NotImplementedError

    async def _enable_rx_acks(self) -> None:
        raise NotImplementedError

    async def _disable_rx_acks(self) -> None:
        raise NotImplementedError
