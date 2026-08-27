"""MT7925AU firmware loader.

Port of the mt7925u cold-boot download path: mt792xu_mcu_power_on, mt792xu_dma_init
(mt792x_usb.c), then mt7925_run_firmware -> mt792x_load_firmware -> connac2 patch +
RAM upload + FW_START (mt76_connac_mcu.c). Verified against
driver_captures/captures_mt7925u/ with scripts/chips/mt7925au/verify_pcap.py.

The firmware container is the connac2 format, reused verbatim by mt7925. The
connac3-specific bits live in mcu.py (txd[1]) and mcu.init_download (the extra
0xe0002800 PATCH_START address).
"""
import asyncio
import logging
import struct
from pathlib import Path

import usb.core
import usb.util

from wifit3.errors import BringUpError, is_permission_error

from . import mcu
from .transport import MT7925AUTransport
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


class MT7925AUFirmwareLoader:
    """Two-stage uploader: ROM patch first, then the WM RAM blob, then FW_START."""

    def __init__(self, transport: MT7925AUTransport, assets_dir: Path):
        self.transport = transport
        self.assets_dir = assets_dir

    def _claim_vendor_interface(self, clear_halts: bool = True) -> "int | None":
        """Find + claim the vendor-specific (class 0xFF) interface that owns the bulk
        endpoints (its number differs per unit). With clear_halts, clear-halt the bulk
        endpoints once."""
        dev = self.transport.dev
        try:
            for intf in dev.get_active_configuration():
                if intf.bInterfaceClass == 0xFF:
                    try:
                        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                            dev.detach_kernel_driver(intf.bInterfaceNumber)
                    except (NotImplementedError, usb.core.USBError) as e:
                        logger.debug("kernel-driver detach skipped: %s", e)
                    usb.util.claim_interface(dev, intf.bInterfaceNumber)
                    if clear_halts:
                        for ep in (EP_OUT_FW, EP_OUT_MCU, EP_IN_BULK, EP_IN_MCU):
                            self.transport.clear_halt(ep)
                    return intf.bInterfaceNumber
        except Exception as e:
            if is_permission_error(e):
                raise
            logger.debug(f"vendor-interface claim: {e}")
        return None

    async def load_firmware(self) -> bool:
        logger.debug("Starting MT7925AU firmware upload...")
        iface = self._claim_vendor_interface()
        logger.debug(f"Claimed vendor-specific interface {iface}")
        await asyncio.sleep(0.2)

        # Probe pre-DMA (mt7925u_probe): chip-id then hw-rev (mdev->rev is built from
        # both, usb.c:197-198), then MT_CONN_ON_MISC for the warm guard.
        try:
            chip_id = self.transport.read_reg32(MT_HW_CHIPID)
            hw_rev = self.transport.read_reg32(MT_HW_REV)
        except usb.core.USBError as e:
            if is_permission_error(e):
                raise BringUpError("permissions", str(e)) from e
            logger.error(f"Chip-id read failed ({e}). Control endpoint unresponsive — "
                         "the chip is warm or wedged. PLEASE PHYSICALLY REPLUG the card.")
            return False
        if (chip_id & 0xFFFF) != MT_CHIP_ID_EXPECTED:
            logger.error(f"Unexpected chip ID 0x{chip_id:x} (expected lower 16 = "
                         f"0x{MT_CHIP_ID_EXPECTED:x})")
            return False
        logger.info(f"MT7925 detected: chip_id=0x{chip_id:x} rev=0x{hw_rev & 0xff:02x}")

        misc = self.transport.read_reg32(MT_CONN_ON_MISC)
        if (misc & MT_TOP_MISC2_FW_N9_RDY) != 0:
            logger.error(f"load_firmware reached on a warm chip (MT_CONN_ON_MISC=0x{misc:x}); "
                         "warm chips must take the reattach path. Aborting.")
            return False

        # mt792xu_mcu_power_on: MT_VEND_POWER_ON (OUT, wValue 0, wIndex 1), then poll.
        logger.debug("MCU power-on...")
        self.transport.dev.ctrl_transfer(
            bmRequestType=MT_REQ_OUT_VENDOR, bRequest=MT_VEND_POWER_ON,
            wValue=0x0000, wIndex=0x0001, data_or_wLength=b"")
        if not await self._poll_reg(MT_CONN_ON_MISC, MT_TOP_MISC2_FW_PWR_ON,
                                    MT_TOP_MISC2_FW_PWR_ON, attempts=50, delay=0.01):
            logger.error("MCU power-on timeout")
            return False

        # Start the RX reader before dma_init enables RX_DMA (kernel: alloc_queues
        # precedes dma_init) so a read is posted on EP 0x84 when RX_DMA comes up.
        self.transport.start_rx()
        self._dma_init()

        # mt7925u_mcu_init: mt76_set(MT_UDMA_TX_QSEL, MT_FW_DL_EN) before any MCU cmd.
        self.transport.set_bits(MT_UDMA_TX_QSEL, MT_FW_DL_EN)

        if not await self._load_patch():
            return False
        if not await self._load_ram():
            return False

        # mt792x_load_firmware tail: poll FW_N9_RDY (best-effort; the FW_START ack on
        # EP 0x84 is the authoritative boot signal). FW_DL_EN stays set for the
        # run_firmware tail (get_nic_capability etc.), cleared in post_boot_init.
        if await self._poll_reg(MT_CONN_ON_MISC, MT_TOP_MISC2_FW_N9_RDY,
                                MT_TOP_MISC2_FW_N9_RDY, attempts=15, delay=0.1,
                                read_timeout_ms=300):
            logger.debug("FW_N9_RDY confirmed.")
        else:
            logger.warning("FW_N9_RDY unconfirmed over EP0 — FW_START ack already received.")
        logger.debug("MT7925AU firmware ready.")
        return True

    async def _poll_reg(self, addr, mask, expected, attempts=50, delay=0.01,
                        read_timeout_ms=200) -> bool:
        """Poll a unified-bus register until (val & mask) == expected."""
        for _ in range(attempts):
            try:
                val = self.transport._ctrl_read(MT_REQ_IN_VENDOR, MT_VEND_READ_EXT,
                                                addr, timeout=read_timeout_ms)
            except usb.core.USBError:
                val = 0
            if (val & mask) == expected:
                return True
            await asyncio.sleep(delay)
        return False

    # ---- mt792xu_dma_init (mt792x_usb.c) ----------------------------------

    def _dma_init(self, resume: bool = False):
        """Pre-firmware WFDMA bring-up in kernel order (mt792xu_wfdma_init, then WLCFG,
        then dma_rx_evt_ep4 + epctl_rst_opt). Every mt76_set/clear is a read+write.

        ``resume=True`` is the kernel's mt792xu_dma_init(dev, resume) light path: it stops
        after WLCFG (skips rx_evt_ep4 + epctl_rst_opt), used by the warm reattach when the
        WFDMA NEED_REINIT latch says re-init is needed (see driver._warm_reattach)."""
        t = self.transport
        # mt792xu_dma_prefetch — TX-ring prefetch depth + base pointer.
        for idx, cnt, base in MT_DMA_PREFETCH_CONF:
            t.rmw(MT_UWFDMA0_TX_RING_EXT_CTRL(idx),
                  MT_WPDMA0_MAX_CNT_MASK | MT_WPDMA0_BASE_PTR_MASK,
                  (cnt & MT_WPDMA0_MAX_CNT_MASK) | ((base << 16) & MT_WPDMA0_BASE_PTR_MASK))

        # GLO_CFG — enable the WFDMA engines for firmware download.
        t.clear_bits(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_OMIT_RX_INFO)
        t.set_bits(MT_UWFDMA0_GLO_CFG,
                   MT_WFDMA0_GLO_CFG_OMIT_TX_INFO | MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2
                   | MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL
                   | MT_WFDMA0_GLO_CFG_TX_DMA_EN | MT_WFDMA0_GLO_CFG_RX_DMA_EN)

        # DMA scheduler.
        t.rmw(MT_DMASHDL_REFILL, MT_DMASHDL_REFILL_MASK, 0xffe00000)
        t.clear_bits(MT_DMASHDL_PAGE, MT_DMASHDL_GROUP_SEQ_ORDER)
        t.rmw(MT_DMASHDL_PKT_MAX_SIZE,
              MT_DMASHDL_PKT_MAX_SIZE_PLE | MT_DMASHDL_PKT_MAX_SIZE_PSE, 0x1)
        for i in range(5):
            t.write_reg32(MT_DMASHDL_GROUP_QUOTA(i), MT_DMASHDL_GROUP_QUOTA_VAL)
        for i in range(5, 16):
            t.write_reg32(MT_DMASHDL_GROUP_QUOTA(i), 0)
        for n, val in enumerate(MT_DMASHDL_Q_MAP_VALS):
            t.write_reg32(MT_DMASHDL_Q_MAP(n), val)
        for n, val in enumerate(MT_DMASHDL_SCHED_SET_VALS):
            t.write_reg32(MT_DMASHDL_SCHED_SET(n), val)
        t.set_bits(MT_WFDMA_DUMMY_CR, MT_WFDMA_NEED_REINIT)

        # WLCFG_0/_1 — DMA TX/RX enables + 1us tick.
        t.clear_bits(MT_UDMA_WLCFG_0, MT_WL_RX_FLUSH)
        t.set_bits(MT_UDMA_WLCFG_0,
                   MT_WL_RX_EN | MT_WL_TX_EN | MT_WL_RX_MPSZ_PAD0 | MT_TICK_1US_EN)
        t.clear_bits(MT_UDMA_WLCFG_0, MT_WL_RX_AGG_TO | MT_WL_RX_AGG_LMT)
        t.clear_bits(MT_UDMA_WLCFG_1, MT_WL_RX_AGG_PKT_LMT)

        if resume:
            return   # mt792xu_dma_init(resume): stop before rx_evt_ep4 + epctl_rst_opt

        # mt792xu_dma_rx_evt_ep4 — route RX events (MCU resp + FW-up signal) to EP 0x84.
        for _ in range(100):
            if not (t.read_reg32(MT_UWFDMA0_GLO_CFG) & MT_WFDMA0_GLO_CFG_RX_DMA_BUSY):
                break
        t.clear_bits(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_RX_DMA_EN)
        t.set_bits(MT_WFDMA_HOST_CONFIG, MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN)
        t.set_bits(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_RX_DMA_EN)

        self._epctl_rst_opt()

    def _epctl_rst_opt(self):
        """mt792xu_epctl_rst_opt(false): clear the bulk-EP reset-option bits over the
        UHW bus (bRequest 0x01/0x02) — byte-exact to the Linux wire. On WinUSB the UHW
        bus returns Errno 5, so fall back to the unified bus (the register is reachable
        there too)."""
        try:
            v = self.transport.read_reg32_uhw(MT_SSUSB_EPCTL_CSR_EP_RST_OPT)
            self.transport.write_reg32_uhw(MT_SSUSB_EPCTL_CSR_EP_RST_OPT,
                                           v & ~MT_EPCTL_EP_RST_OPT_MASK)
        except usb.core.USBError as e:
            logger.debug(f"UHW epctl failed ({e}); using unified bus (WinUSB).")
            v = self.transport.read_reg32(MT_SSUSB_EPCTL_CSR_EP_RST_OPT)
            self.transport.write_reg32(MT_SSUSB_EPCTL_CSR_EP_RST_OPT,
                                       v & ~MT_EPCTL_EP_RST_OPT_MASK)

    def dma_need_reinit(self) -> bool:
        """mt792x_dma_need_reinit (mt792x.h:357): True when the WFDMA NEED_REINIT latch is
        CLEAR. `_dma_init` sets it, so a warm chip whose firmware is still running reads it
        SET and this returns False (no re-init). The warm reattach gates its light
        dma_init(resume) on this, exactly like mt7921u_resume."""
        return not (self.transport.read_reg32(MT_WFDMA_DUMMY_CR) & MT_WFDMA_NEED_REINIT)

    # ---- ROM patch (mt76_connac2_load_patch) ------------------------------

    async def _load_patch(self) -> bool:
        path = self.assets_dir / FIRMWARE_ROM_PATCH
        if not path.exists():
            logger.error(f"Missing patch firmware: {path}")
            return False
        data = path.read_bytes()
        if len(data) < PATCH_HDR_SIZE:
            logger.error(f"Patch file too small: {len(data)} bytes")
            return False

        # mt76_connac2_patch_hdr — desc.n_region at offset 44 (BE).
        n_region = struct.unpack_from(">I", data, 44)[0]
        if n_region == 0 or n_region > 16:
            logger.error(f"Implausible patch n_region={n_region}")
            return False

        resp = await self.transport.send_mcu_command(*mcu.patch_sem_ctrl(get=True))
        if resp is None:
            logger.error("PATCH_SEM_CONTROL get: no response")
            return False

        try:
            for i in range(n_region):
                sec_off = PATCH_HDR_SIZE + i * PATCH_SEC_SIZE
                sec = data[sec_off:sec_off + PATCH_SEC_SIZE]
                sec_type = struct.unpack_from(">I", sec, 0)[0]
                offs = struct.unpack_from(">I", sec, 4)[0]
                addr = struct.unpack_from(">I", sec, 12)[0]
                length = struct.unpack_from(">I", sec, 16)[0]
                sec_info = struct.unpack_from(">I", sec, 20)[0]   # info.sec_key_idx
                if (sec_type & PATCH_SEC_TYPE_MASK) != PATCH_SEC_TYPE_INFO:
                    continue
                mode = mcu.get_data_mode(sec_info)
                logger.debug(f"Patch section {i}: addr=0x{addr:08x} len={length} mode=0x{mode:08x}")
                ack = await self.transport.send_mcu_command(
                    *mcu.init_download(addr, length, mode),
                    wait_resp=True, resp_timeout_ms=3000)
                if ack is None:
                    logger.error(f"init_download patch section {i}: no ack")
                    return False
                if not await self._send_fw_chunks(data, offs, length, f"patch sec {i}"):
                    return False

            ack = await self.transport.send_mcu_command(
                *mcu.patch_finish(), wait_resp=True, resp_timeout_ms=3000)
            if ack is None:
                logger.error("PATCH_FINISH_REQ: no ack")
                return False
        finally:
            await self.transport.send_mcu_command(*mcu.patch_sem_ctrl(get=False))

        logger.debug("ROM patch loaded.")
        return True

    # ---- WM RAM (mt76_connac2_load_ram) -----------------------------------

    async def _load_ram(self) -> bool:
        path = self.assets_dir / FIRMWARE_WM
        if not path.exists():
            logger.error(f"Missing WM firmware: {path}")
            return False
        data = path.read_bytes()
        if len(data) < FW_TRAILER_SIZE:
            logger.error(f"WM file too small: {len(data)} bytes")
            return False

        # mt76_connac2_fw_trailer at the END of the file; n_region @ offset 2 (u8).
        trailer_off = len(data) - FW_TRAILER_SIZE
        n_region = data[trailer_off + 2]
        if n_region == 0 or n_region > 16:
            logger.error(f"Implausible WM n_region={n_region}")
            return False

        offset = 0
        override_addr = 0
        for i in range(n_region):
            reg_off = trailer_off - (n_region - i) * FW_REGION_SIZE
            reg = data[reg_off:reg_off + FW_REGION_SIZE]
            addr = struct.unpack_from("<I", reg, 16)[0]
            length = struct.unpack_from("<I", reg, 20)[0]
            feature_set = reg[24]
            logger.debug(f"WM region {i}: addr=0x{addr:08x} len={length} "
                        f"feature_set=0x{feature_set:02x}")
            if feature_set & FW_FEATURE_NON_DL:
                offset += length
                continue
            if feature_set & FW_FEATURE_OVERRIDE_ADDR:
                override_addr = addr
            mode = mcu.gen_dl_mode(feature_set)
            ack = await self.transport.send_mcu_command(
                *mcu.init_download(addr, length, mode),
                wait_resp=True, resp_timeout_ms=3000)
            if ack is None:
                logger.error(f"init_download WM region {i}: no ack")
                return False
            if not await self._send_fw_chunks(data, offset, length, f"WM region {i}"):
                return False
            offset += length

        resp = await self.transport.send_mcu_command(
            *mcu.fw_start(override_addr), wait_resp=True, resp_timeout_ms=5000)
        if resp is None:
            logger.error("FW_START_REQ: MCU_EVENT_FW_START never arrived")
            return False
        logger.info("Firmware booted: FW_START ack received.")
        return True

    async def _send_fw_chunks(self, blob: bytes, offset: int, length: int, label: str) -> bool:
        sent = 0
        while sent < length:
            cur = min(MAX_FW_CHUNK, length - sent)
            chunk = blob[offset + sent: offset + sent + cur]
            if not await self.transport.send_fw_chunk(chunk, timeout_ms=5000):
                logger.error(f"FW_SCATTER failed for {label} at offset {sent}/{length}")
                return False
            sent += cur
            await asyncio.sleep(0)
        return True
