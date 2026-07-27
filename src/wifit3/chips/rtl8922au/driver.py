"""RTL8922AU driver: Realtek RTL8922A (802.11be) over USB, ported from rtw89-7.2.
See RTL8922AU.md.
"""
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from ..driver import Driver, DeviceID, ProgressCallback
from . import coex, mac, phy, rfk
from .constants import (
    R_BE_PAD_CTRL2, _LIBUSB_SPEED_SUPER, USB_SWITCH_DELAY, B_BE_MATCH_CNT,
    B_BE_RSM_EN_V1, B_BE_NO_PDN_CHIPOFF_V1, B_BE_USB_AUTO_INSTALL_MASK, B_BE_USB23_SW_MODE,
    B_BE_USB3_FORCE, B_BE_USB2_FORCE, B_BE_FORCE_U3_CK, B_BE_FORCE_U2_CK, B_BE_FORCE_CLK_U2,
    B_BE_USB3_GEN_MODE, B_BE_USB3_LANE_MODE, BULKOUT_ID_H2C,
)
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
        self._h2c_ep: Optional[int] = None

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
            self._discover_bulkout(intf)
            return intf.bInterfaceNumber
        return None

    def _discover_bulkout(self, intf) -> None:
        """Map DMA channels to bulk-OUT endpoints: out_pipe is the interface's bulk-OUT endpoint
        addresses in order; the H2C channel uses out_pipe[bulkout_id[DMA_H2C]]. [SRC] usb.c:1030-1056,
        rtw8922au.c:27 (bulkout_id[DMA_H2C]=2)."""
        out_pipe = [ep.bEndpointAddress for ep in intf
                    if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT
                    and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        if len(out_pipe) > BULKOUT_ID_H2C:
            self._h2c_ep = out_pipe[BULKOUT_ID_H2C]

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold-boot bring-up: claim the vendor interface, run the USB mode switch, read the
        chip version/id. [SRC] usb.c rtw89_usb_probe, core.c rtw89_read_chip_ver."""
        iface = self._claim_vendor_interface()
        logger.info("RTL8922AU: claimed vendor interface %s", iface)
        self._switch_usb_mode()
        ver = mac.read_chip_ver(self.transport)
        logger.info("RTL8922AU: cv=0x%x acv=0x%x cid=0x%x aid=0x%x",
                    ver["cv"], ver["acv"], ver["cid"], ver["aid"])
        mac.mac_pwr_on(self.transport, ver["cv"])
        # rtw89_chip_info_setup continues: wait_firmware_completion + fw_recognize are file-side
        # (no wire ops), then chip_efuse_info_setup -> mac_partial_init. [SRC] core.c:7367-7423.
        mac.partial_init(self.transport, self._h2c_ep, ver["cv"])
        # chip_efuse_info_setup continues after partial_init: dump the logical efuse + phycap.
        # [SRC] core.c:7268-7291.
        mac.parse_efuse_map(self.transport, ver["cv"])
        mac.parse_phycap_map(self.transport, ver["cv"])
        mac.setup_phycap(self.transport)          # H2C phy-capability query to the running fw
        # chip_info_setup's out: path powers the MAC back off. rtw89_core_start re-powers it.
        # [SRC] core.c:7419-7422.
        mac.mac_pwr_off(self.transport)
        # rtw89_core_register_hw tail: the rfkill GPIO polling init closes out probe.
        # [SRC] core.c:7582.
        mac.rfkill_polling_init(self.transport)
        # Interface-up path: rtw89_ops_start -> rtw89_core_start -> rtw89_mac_preinit (the second
        # pwr_on, then mac_func_en). [SRC] core.c:6626-6635, mac.c:4341-4357.
        mac.mac_preinit(self.transport, ver["cv"])
        # phy_init_bb_afe applies a firmware AFE table; this card ships no afe element, so it is a
        # no-op. Then rtw89_mac_init: partial_init(include_bb=True). [SRC] core.c:6640-6648, phy.c:1968.
        mac.mac_init(self.transport, self._h2c_ep, ver["cv"])
        # core_start resumes after mac_init: btc_ntfy_poweron + chip_reset_bb_rf are no-ops on BE,
        # then phy_init_bb_reg writes the firmware BB register tables. [SRC] core.c:6648-6659.
        phy.init_bb_reg(self.transport, ver["cv"])
        phy.chip_bb_postinit(self.transport)      # rtw8922a_bb_postinit PHY_0+PHY_1. core.c:6660
        phy.init_rf_reg(self.transport, self._h2c_ep, ver["cv"])   # RF radio tables. core.c:6662
        coex.ntfy_init(self.transport, self._h2c_ep, ver["cv"])    # btc_ntfy_init. core.c:6664
        phy.dm_init(self.transport, ver["cv"])    # phy_dm_init BB inits (pre-RFK). core.c:6665
        phy.rfk_hw_init(self.transport)           # chip_rfk_hw_init (syn/ktbl/pll). phy.c:8256
        phy.init_rf_nctl(self.transport, ver["cv"])   # preinit + RF_NCTL fw table. phy.c:8257
        # rfk_init is software-only. Then set_txpwr_ctrl + power_trim + cfg_txrx_path. phy.c:8259-8262.
        phy.set_txpwr_ctrl(self.transport)
        phy.power_trim(self.transport)
        phy.bb_cfg_txrx_path(self.transport)
        # core_start tail: edcca-bands (8922A no-op), ppdu/phy-rpt/rts band cfgs, rfk_init_late.
        # [SRC] core.c:6667-6685.
        mac.cfg_ppdu_status_bands(self.transport)
        mac.cfg_phy_rpt_bands(self.transport)
        mac.update_rts_threshold(self.transport)
        rfk.rfk_init_late(self.transport, self._h2c_ep)
        return True

    def _switch_usb_mode(self) -> None:
        """rtw89_usb_switch_mode: SuperSpeed (USB 3 / USB-C) needs no switch; USB 2 runs the
        BE mode switch. [SRC] usb.c:1172-1189."""
        if getattr(self.dev, "speed", None) == _LIBUSB_SPEED_SUPER:
            return
        self._switch_mode_be()

    def _switch_mode_be(self) -> None:
        """rtw89_usb_switch_mode_be: read PAD_CTRL2; return if already switched (a USB 2 port
        that ran this before), else force USB 2/3 mode. [SRC] usb.c:1143-1170."""
        pad = self.transport.read32(R_BE_PAD_CTRL2)
        if mac.field_get(B_BE_MATCH_CNT, pad) == USB_SWITCH_DELAY:
            return
        # TODO: verify, untested here. The cold-boot capture was already switched, so the
        # force-mode write below does not fire. [SRC] usb.c:1156-1167.
        pad = (pad & ~B_BE_MATCH_CNT) | mac.field_prep(B_BE_MATCH_CNT, USB_SWITCH_DELAY)
        pad |= (B_BE_RSM_EN_V1 | B_BE_NO_PDN_CHIPOFF_V1
                | B_BE_USB_AUTO_INSTALL_MASK | B_BE_USB23_SW_MODE)
        pad &= ~(B_BE_USB3_FORCE | B_BE_USB2_FORCE | B_BE_FORCE_U3_CK | B_BE_FORCE_U2_CK
                 | B_BE_FORCE_CLK_U2 | B_BE_USB3_GEN_MODE | B_BE_USB3_LANE_MODE)
        self.transport.write32_quiet(R_BE_PAD_CTRL2, pad)

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
