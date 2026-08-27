"""
MT7921AU firmware loader.

Verified against driver_captures/captures_mt7921u/capture-3.pcap and
driver_sources/mt76-source-v6.18/mt76_connac_mcu.c (mt76_connac2_load_patch,
mt76_connac_mcu_send_ram_firmware).

See src/wifit3/chips/mt7921au/MT7921AU.md for wire-format details.
"""
import logging
import struct
import asyncio
import usb.core
import usb.util
from pathlib import Path

from wifit3.errors import BringUpError, is_permission_error

from .transport import MT7921AUTransport
# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


class MT7921AUFirmwareLoader:
    """Multi-stage firmware uploader: ROM patch first, then WM RAM blob."""

    def __init__(self, transport: MT7921AUTransport, assets_dir: Path):
        self.transport = transport
        self.assets_dir = assets_dir

    def _claim_vendor_interface(self, clear_halts: bool = True) -> "int | None":
        """Find + claim the vendor-specific (class 0xFF) interface that owns the
        bulk endpoints. Returns its number, or None.

        With clear_halts (the default) it also clear-halts the bulk endpoints. The
        warm re-attach passes clear_halts=False for its pre-reset claim so the
        freshly-enumerated pipes are clear-halted exactly ONCE — by the subsequent
        load_firmware. A second clear_halt on the re-enumerated pipes desyncs them
        on WinUSB and wedges the device (see MT7921AU.md "Warm re-attach")."""
        dev = self.transport.dev
        try:
            for intf in dev.get_active_configuration():
                if intf.bInterfaceClass == 0xFF:
                    try:
                        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                            dev.detach_kernel_driver(intf.bInterfaceNumber)
                            logger.info("detached kernel driver from interface %d",
                                        intf.bInterfaceNumber)
                    except (NotImplementedError, usb.core.USBError) as e:
                        logger.debug("kernel-driver detach skipped: %s", e)
                    usb.util.claim_interface(dev, intf.bInterfaceNumber)
                    if clear_halts:
                        for ep in (EP_OUT_FW, EP_OUT_MCU, EP_IN_BULK, EP_IN_MCU):
                            self.transport.clear_halt(ep)
                    return intf.bInterfaceNumber
        except Exception as e:
            # A permission failure (no udev access / node not writable) must NOT be swallowed: it
            # means the card needs setup, and the engine offers the install when it propagates.
            if is_permission_error(e):
                raise
            logger.debug(f"vendor-interface claim: {e}")
        return None

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    async def load_firmware(self) -> bool:
        logger.debug("Starting MT7921AU firmware upload sequence...")

        # Claim the vendor-specific (class 0xFF) interface that owns the bulk EPs.
        # Its number differs per unit (PAU0F=0, AWUS036AXML=3), so detect it.
        # NOTE: do NOT call set_configuration() on Windows / WinUSB. It resets the
        # host-side data toggles to zero but does NOT send a SET_CONFIG over the
        # wire, so the device-side toggles stay put; they then desync after ~4
        # packets and the bulk OUT pipe silently NAKs everything afterward.
        iface = self._claim_vendor_interface()
        logger.debug(f"Claimed vendor-specific interface {iface}")
        await asyncio.sleep(0.2)

        # Chip-id read doubles as a control-endpoint liveness check. A COLD chip
        # answers it (0x7961); once firmware is running, EP0 control transfers can
        # stop being serviced and this times out. A USB reset does NOT re-cold-boot
        # the chip in userland on Windows (verified — it re-acquires at the same
        # address, still unresponsive), so a timeout here means a physical replug is
        # the only path to a fresh boot. Fail fast with that guidance instead of
        # hammering a warm/wedged chip (which only makes it worse).
        try:
            chip_id = self.transport.read_reg32(MT_CHIP_ID_ADDR)
        except usb.core.USBError as e:
            if is_permission_error(e):
                # Not warm/wedged: we lack access to the node. Propagate so the engine offers setup.
                raise BringUpError("permissions", str(e)) from e
            logger.error(
                f"Chip-id read failed ({e}). The control endpoint is unresponsive — "
                "the chip is warm or wedged from a prior boot, which userland cannot "
                "reset on Windows. PLEASE PHYSICALLY REPLUG the card for a cold boot."
            )
            return False
        if (chip_id & 0xFFFF) != MT_CHIP_ID_EXPECTED:
            logger.error(f"Unexpected chip ID 0x{chip_id:x} (expected lower 16 = 0x{MT_CHIP_ID_EXPECTED:x})")
            return False
        logger.info(f"MT7921 detected: chip_id=0x{chip_id:x}")

        # Safety net only: driver.connect() routes a warm chip to _warm_reattach (the
        # light reattach), so load_firmware is normally reached only on a cold chip
        # (MISC==0). If a warm chip ever reaches here, do NOT cold-boot it — the
        # mcu_power_on below poisons a warm chip's bulk pipes on WinUSB (no pre-reset
        # usb_reset_device is available). Bail instead. See MT7921AU.md "Warm re-attach".
        misc = self.transport.read_reg32_unified(MT_CONN_ON_MISC)
        if (misc & MT_TOP_MISC2_FW_N9_RDY) != 0:
            logger.error(
                f"load_firmware reached on a warm chip (MT_CONN_ON_MISC=0x{misc:x}); "
                "warm chips must take the _warm_reattach path, not a cold boot. Aborting."
            )
            return False

        logger.debug("Sending MCU power-on...")
        self.transport.send_vendor_request(
            MT_VEND_WRITE_RECIPIENT, MT_VEND_POWER_ON, 0x0000, 0x0001
        )

        if not await self._poll_reg(MT_CONN_ON_MISC, MT_TOP_MISC2_FW_PWR_ON,
                                    MT_TOP_MISC2_FW_PWR_ON, attempts=50, delay=0.01):
            logger.error("MCU power-on timeout")
            return False
        logger.debug("MCU powered on.")

        # Start the single RX reader BEFORE dma_init enables RX_DMA — mirrors the
        # kernel order (mt76u_alloc_queues before mt792xu_dma_init) so a read is
        # posted on EP 0x84 by the time RX_DMA comes up. It stays running through
        # the post-boot init and operational RX; the driver stops it at close().
        self.transport.start_rx()

        self._dma_init()

        # MT_SWDEF_MODE = NORMAL before firmware download (mt7921/usb.c:118).
        self.transport.write_reg32_unified(MT_SWDEF_MODE, MT_SWDEF_NORMAL_MODE)

        # mt76_set(MT_UDMA_TX_QSEL, MT_FW_DL_EN): read-modify-write (the kernel helper
        # always emits the write), enabling the FW-download path. [SRC] mt7921/usb.c:119.
        # A bare write + readback here diverges from the wire's RMW read-then-write order.
        self._rmw(MT_UDMA_TX_QSEL, 0, MT_FW_DL_EN)

        if not await self._load_patch():
            return False

        # Mirror Linux behavior observed in capture-3 between patch and RAM
        # upload (~89 ms after PATCH_SEM_RELEASE): two boot-status reads with
        # wValue=0x30, wLength=64. Source unknown (possibly btusb concurrent
        # init, possibly mt76 reset path). Cheap to replicate, side-effect-free.
        for _ in range(2):
            self.transport.read_boot_status(length=64)

        # Drain any MCU responses that arrived during patch upload, so we can
        # see what the device sent back (now that DL_MODE_NEED_RSP bit is correct).
        drained = 0
        while not self.transport._mcu_rx_queue.empty():
            resp = self.transport._mcu_rx_queue.get_nowait()
            logger.debug(f"post-patch MCU drain msg {drained} ({len(resp)} B): {resp[:32].hex()}{'...' if len(resp)>32 else ''}")
            drained += 1
        logger.debug(f"post-patch MCU drain: {drained} response(s) consumed")

        if not await self._load_ram():
            # _load_ram waits for the FW_START boot event (MCU_EVENT_FW_START,
            # eid=0x01, on EP 0x84). If it never arrived, the firmware did not
            # start — dump the EP0/queue post-mortem and bail.
            self._log_boot_diagnostics()
            return False

        # Boot event received: firmware has started. FW_N9_RDY (the "fully
        # ready" bit, read over EP0) is a best-effort secondary confirmation —
        # EP0 can be briefly unresponsive right after the handoff, and the
        # bulk-IN boot event is the authoritative signal.
        if await self._poll_reg(MT_CONN_ON_MISC, MT_TOP_MISC2_FW_N9_RDY,
                                MT_TOP_MISC2_FW_N9_RDY,
                                attempts=15, delay=0.1, read_timeout_ms=300):
            logger.debug("FW_N9_RDY confirmed over EP0.")
        else:
            logger.warning("FW_N9_RDY unconfirmed over EP0 — boot event already "
                           "received, continuing.")

        # FW_DL_EN stays set here. The kernel clears it in mt7921u_mcu_init only
        # AFTER mt7921_run_firmware's tail (get_nic_capability + fw_log_2_host),
        # so the post-boot bring-up (init.post_boot_init) clears it in wire order.
        logger.debug("MT7921AU firmware ready (FW_DL_EN still set for run_firmware tail).")
        return True

    def _log_boot_diagnostics(self):
        """Post-mortem when the FW_START boot event never arrives: probe whether
        EP0 still answers (the documented Windows symptom is EP0 going dead at the
        handoff) and dump any late bulk-IN responses still queued."""
        logger.error("MCU_EVENT_FW_START never arrived — EP0 liveness diagnostics:")
        try:
            chip = self.transport.read_reg32(MT_CHIP_ID_ADDR)
            logger.error(f"  standard-bus MT_HW_CHIPID = 0x{chip:08x} (cold value 0x7961xxxx)")
        except Exception as e:
            logger.error(f"  standard-bus MT_HW_CHIPID FAILED: {e}")
        try:
            misc = self.transport.read_reg32_unified(MT_CONN_ON_MISC)
            logger.error(f"  unified MT_CONN_ON_MISC = 0x{misc:08x}")
        except Exception as e:
            logger.error(f"  unified MT_CONN_ON_MISC FAILED: {e}")
        q = self.transport._mcu_rx_queue
        logger.error(f"  bulk-IN queue depth: {q.qsize()}")
        n = 0
        while not q.empty():
            try:
                r = q.get_nowait()
            except Exception:
                break
            eid = r[28] if len(r) > 28 else None
            sq = r[29] if len(r) > 29 else None
            logger.error(f"    late bulk-IN {n}: eid=0x{eid:02x} seq=0x{sq:02x} ({len(r)} B)")
            n += 1

    def _rmw(self, addr: int, clear_mask: int, set_mask: int):
        """Read-modify-write a unified-bus register."""
        val = self.transport.read_reg32_unified(addr)
        new = (val & ~clear_mask) | set_mask
        self.transport.write_reg32_unified(addr, new)

    def _dma_init(self, resume: bool = False):
        """Port of mt792xu_dma_init (mt792x_usb.c) — pre-firmware WFDMA bring-up,
        in kernel order: dma_prefetch, GLO_CFG enables, DMASHDL, DUMMY_CR
        (mt792xu_wfdma_init), then WLCFG, then (cold only) rx_evt_ep4 + epctl_rst_opt.

        ``resume=True`` is the kernel's mt792xu_dma_init(dev, resume) light path: it
        stops after WLCFG (skips rx_evt_ep4 + epctl_rst_opt), used by the warm
        reattach when the DMA needs re-initialising (see driver._warm_reattach).

        The IN URB pool must already be posted before this runs (see
        load_firmware): GLO_CFG sets RX_DMA_EN, and with no RX URBs queued the RX
        path backs up and stalls the firmware-download bulk OUT.
        """
        # mt792xu_dma_prefetch — TX-ring prefetch depth + base pointer.
        for idx, cnt, base in MT_DMA_PREFETCH_CONF:
            self._rmw(MT_UWFDMA0_TX_RING_EXT_CTRL(idx),
                      MT_WPDMA0_MAX_CNT_MASK | MT_WPDMA0_BASE_PTR_MASK,
                      (cnt & MT_WPDMA0_MAX_CNT_MASK)
                      | ((base << 16) & MT_WPDMA0_BASE_PTR_MASK))

        # MT_UWFDMA0_GLO_CFG — enable the WFDMA engines for firmware download.
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_OMIT_RX_INFO, 0)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0,
                  MT_WFDMA0_GLO_CFG_OMIT_TX_INFO | MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2
                  | MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL
                  | MT_WFDMA0_GLO_CFG_TX_DMA_EN | MT_WFDMA0_GLO_CFG_RX_DMA_EN)

        # DMA scheduler group quotas / queue maps.
        self._dmashdl_init()
        self._rmw(MT_WFDMA_DUMMY_CR, 0, MT_WFDMA_NEED_REINIT)

        # WLCFG_0/_1 — DMA TX/RX enables + 1us tick.
        self._rmw(MT_UDMA_WLCFG_0, MT_WL_RX_FLUSH, 0)
        self._rmw(MT_UDMA_WLCFG_0, 0,
                  MT_WL_RX_EN | MT_WL_TX_EN | MT_WL_RX_MPSZ_PAD0 | MT_TICK_1US_EN)
        self._rmw(MT_UDMA_WLCFG_0, MT_WL_RX_AGG_TO | MT_WL_RX_AGG_LMT, 0)
        self._rmw(MT_UDMA_WLCFG_1, MT_WL_RX_AGG_PKT_LMT, 0)

        if resume:
            return  # kernel mt792xu_dma_init(resume): stop before rx_evt_ep4 + epctl

        self._rx_evt_ep4()
        self._epctl_rst_opt()

    def dma_need_reinit(self) -> bool:
        """mt792x_dma_need_reinit: True when the WFDMA NEED_REINIT latch is clear.
        _dma_init sets it, so a warm chip whose firmware is still running reads it
        SET and this returns False (no re-init needed). The warm reattach gates its
        light dma_init(resume) on this, exactly like mt7921u_resume."""
        return not (self.transport.read_reg32_unified(MT_WFDMA_DUMMY_CR)
                    & MT_WFDMA_NEED_REINIT)

    def _epctl_rst_opt(self):
        """mt792xu_dma_rx_evt_ep4's sibling epctl_rst_opt(false): clear the
        bulk-endpoint reset-option bits in MT_SSUSB_EPCTL_CSR_EP_RST_OPT. The
        kernel uses the UHW bus (Errno 5 on WinUSB); the register is reachable over
        the unified bus, which works on WinUSB. The bits read SET on a cold device,
        so leaving them set holds the bulk EPs in a reset-option state the kernel
        clears before firmware boot."""
        v = self.transport.read_reg32_unified(MT_SSUSB_EPCTL_CSR_EP_RST_OPT)
        new = v & ~MT_EPCTL_EP_RST_OPT_MASK
        self.transport.write_reg32_unified(MT_SSUSB_EPCTL_CSR_EP_RST_OPT, new)

    def _rx_evt_ep4(self):
        """mt792xu_dma_rx_evt_ep4 — route RX events (the firmware-up signal and
        MCU command responses) to EP 0x84, re-toggling RX DMA across the change.
        Responses then arrive on EP 0x84 — which is why the RX reader only needs
        that one endpoint."""
        for _ in range(100):
            if not (self.transport.read_reg32_unified(MT_UWFDMA0_GLO_CFG)
                    & MT_WFDMA0_GLO_CFG_RX_DMA_BUSY):
                break
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_RX_DMA_EN, 0)
        self._rmw(MT_WFDMA_HOST_CONFIG, 0, MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0, MT_WFDMA0_GLO_CFG_RX_DMA_EN)

    def _dmashdl_init(self):
        """DMA scheduler initialization. Matches mt792xu_wfdma_init's DMASHDL
        block byte-for-byte (constants from mt792x_usb.c:153-173).

        Group quotas: groups 0-4 get min=0x3, max=0xfff; groups 5-15 are zeroed.
        Q_MAP/SCHED_SET values are magic constants from upstream — they map
        priority queues onto scheduler groups for the connac2 TX path.
        """
        # REFILL: top 16 bits = 0xffe0
        self._rmw(MT_DMASHDL_REFILL, MT_DMASHDL_REFILL_MASK, 0xffe00000)
        # Clear GROUP_SEQ_ORDER in PAGE
        self._rmw(MT_DMASHDL_PAGE, MT_DMASHDL_GROUP_SEQ_ORDER, 0)
        # PKT_MAX_SIZE: PLE=1, PSE=0
        self._rmw(MT_DMASHDL_PKT_MAX_SIZE,
                  MT_DMASHDL_PKT_MAX_SIZE_PLE | MT_DMASHDL_PKT_MAX_SIZE_PSE,
                  1)  # PLE field at bit 0, value 1; PSE field cleared

        # Group quotas — Linux's loop bodies.
        # min field = bits 11:0, max field = bits 27:16
        for i in range(5):
            self.transport.write_reg32_unified(
                MT_DMASHDL_GROUP_QUOTA(i),
                (0xfff << MT_DMASHDL_GROUP_QUOTA_MAX_SHIFT) | (0x3 << MT_DMASHDL_GROUP_QUOTA_MIN_SHIFT),
            )
        for i in range(5, 16):
            self.transport.write_reg32_unified(MT_DMASHDL_GROUP_QUOTA(i), 0)

        # Q_MAP magic constants
        self.transport.write_reg32_unified(MT_DMASHDL_Q_MAP(0), 0x32013201)
        self.transport.write_reg32_unified(MT_DMASHDL_Q_MAP(1), 0x32013201)
        self.transport.write_reg32_unified(MT_DMASHDL_Q_MAP(2), 0x55555444)
        self.transport.write_reg32_unified(MT_DMASHDL_Q_MAP(3), 0x55555444)

        # SCHED_SET magic constants
        self.transport.write_reg32_unified(MT_DMASHDL_SCHED_SET(0), 0x76540132)
        self.transport.write_reg32_unified(MT_DMASHDL_SCHED_SET(1), 0xFEDCBA98)

    async def _poll_reg(self, addr: int, mask: int, expected: int,
                        attempts: int = 50, delay: float = 0.01,
                        read_timeout_ms: int = 200) -> bool:
        """
        Poll a unified-bus register until (val & mask) == expected.

        read_timeout_ms is the per-attempt control-transfer timeout. After
        FW_START_REQ the USB controller can be unresponsive for a few hundred
        ms while firmware boots — keep this short so we fail fast and retry.
        Inline the read to override the default 1s read_reg32_unified timeout.
        """
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        for _ in range(attempts):
            res = self.transport.read_vendor_request(
                MT_VEND_READ_RECIPIENT, MT_VEND_READ_REG_REQ,
                wValue, wIndex, 4, timeout=read_timeout_ms,
            )
            if len(res) >= 4:
                val = struct.unpack("<I", res)[0]
                if (val & mask) == expected:
                    return True
            await asyncio.sleep(delay)
        return False

    # ------------------------------------------------------------------
    # ROM patch upload (mt76_connac2_load_patch equivalent)
    # ------------------------------------------------------------------

    async def _load_patch(self) -> bool:
        path = self.assets_dir / FIRMWARE_ROM_PATCH
        if not path.exists():
            logger.error(f"Missing patch firmware: {path}")
            return False
        data = path.read_bytes()
        if len(data) < PATCH_HDR_SIZE:
            logger.error(f"Patch file too small: {len(data)} bytes")
            return False

        # Parse mt76_connac2_patch_hdr — BE fields.
        # desc.n_region at offset 44 (BE u32).
        n_region = struct.unpack(">I", data[44:48])[0]
        build_date = data[0:16].rstrip(b"\x00").decode("ascii", errors="replace")
        logger.info(f"Patch: build_date={build_date!r} n_region={n_region}")
        if n_region == 0 or n_region > 16:
            logger.error(f"Implausible patch n_region={n_region}")
            return False

        # 1. Acquire patch semaphore.
        resp = await self.transport.send_mcu_command(
            MCU_CMD_PATCH_SEM_CONTROL,
            struct.pack("<I", PATCH_SEM_GET),
        )
        if resp is None:
            logger.error("PATCH_SEM_CONTROL get: no response")
            return False

        try:
            # 2. For each section, send PATCH_START_REQ then FW_SCATTER chunks.
            for i in range(n_region):
                sec_off = PATCH_HDR_SIZE + i * PATCH_SEC_SIZE
                sec = data[sec_off:sec_off + PATCH_SEC_SIZE]
                # mt76_connac2_patch_sec — BE
                # type @ 0, offs @ 4, size @ 8, info.addr @ 12, info.len @ 16
                offs = struct.unpack(">I", sec[4:8])[0]
                addr = struct.unpack(">I", sec[12:16])[0]
                length = struct.unpack(">I", sec[16:20])[0]
                logger.debug(f"Patch section {i}: addr=0x{addr:08x} len={length} offs=0x{offs:x}")

                # The chip answers PATCH_START_REQ with a generic ack (eid=0x01) on
                # EP 0x84. Wait for it before streaming the section — the kernel's
                # lockstep; the chip stops acking if commands outrun it.
                ack = await self.transport.send_mcu_command(
                    MCU_CMD_PATCH_START_REQ,
                    struct.pack("<III", addr, length, DL_MODE_NEED_RSP),
                    wait_resp=True, resp_timeout_ms=3000,
                )
                if ack is None:
                    logger.error(f"PATCH_START_REQ section {i}: no ack")
                    return False

                if not await self._send_fw_chunks(data, offs, length, label=f"patch sec {i}"):
                    return False

            # 3. Close patch session; the chip acks (eid=0x01) after its CRC pass.
            ack = await self.transport.send_mcu_command(
                MCU_CMD_PATCH_FINISH_REQ,
                struct.pack("<I", 0),  # check_crc = 0
                wait_resp=True, resp_timeout_ms=3000,
            )
            if ack is None:
                logger.error("PATCH_FINISH_REQ: no ack")
                return False
        finally:
            # 4. Always release semaphore.
            await self.transport.send_mcu_command(
                MCU_CMD_PATCH_SEM_CONTROL,
                struct.pack("<I", PATCH_SEM_RELEASE),
            )

        logger.debug("ROM patch loaded.")
        return True

    # ------------------------------------------------------------------
    # WM RAM upload (mt76_connac_mcu_send_ram_firmware equivalent)
    # ------------------------------------------------------------------

    async def _load_ram(self) -> bool:
        path = self.assets_dir / FIRMWARE_WM
        if not path.exists():
            logger.error(f"Missing WM firmware: {path}")
            return False
        data = path.read_bytes()
        if len(data) < FW_TRAILER_SIZE:
            logger.error(f"WM file too small: {len(data)} bytes")
            return False

        # mt76_connac2_fw_trailer sits at the END of the file.
        trailer_off = len(data) - FW_TRAILER_SIZE
        trailer = data[trailer_off:]
        # chip_id @ 0, eco_code @ 1, n_region @ 2, format_ver @ 3, format_flag @ 4
        # mt76_connac2_fw_trailer: chip_id@0 eco@1 n_region@2 ... fw_ver[10]@7 build_date[15]@17
        n_region = trailer[2]
        fw_ver = trailer[7:17].rstrip(b"\x00").decode("ascii", errors="replace")
        build_date = trailer[17:32].rstrip(b"\x00").decode("ascii", errors="replace")
        logger.debug(f"WM: chip_id=0x{trailer[0]:02x} n_region={n_region} ver={fw_ver!r} build={build_date!r}")
        if n_region == 0 or n_region > 16:
            logger.error(f"Implausible WM n_region={n_region}")
            return False

        # Regions sit immediately before the trailer:
        #   region[i] @ (trailer_off - (n_region - i) * FW_REGION_SIZE)
        # The raw data starts at offset 0 and is read sequentially across regions.
        offset = 0
        override_addr = 0
        for i in range(n_region):
            reg_off = trailer_off - (n_region - i) * FW_REGION_SIZE
            reg = data[reg_off:reg_off + FW_REGION_SIZE]
            # All __le32 / u8 fields
            addr = struct.unpack("<I", reg[16:20])[0]
            length = struct.unpack("<I", reg[20:24])[0]
            feature_set = reg[24]
            logger.debug(f"WM region {i}: addr=0x{addr:08x} len={length} feature_set=0x{feature_set:02x}")

            if feature_set & FW_FEATURE_NON_DL:
                logger.debug("  -> NON_DL, skipping upload")
                offset += length
                continue

            if feature_set & FW_FEATURE_OVERRIDE_ADDR:
                override_addr = addr

            # The chip acks TARGET_ADDRESS_LEN_REQ (eid=0x01) on EP 0x84 before it
            # will take the region's data — in the capture the ack precedes the
            # FW_SCATTER chunks. Wait for it, then stream (the kernel's lockstep).
            ack = await self.transport.send_mcu_command(
                MCU_CMD_TARGET_ADDRESS_LEN_REQ,
                struct.pack("<III", addr, length, DL_MODE_NEED_RSP),
                wait_resp=True, resp_timeout_ms=3000,
            )
            if ack is None:
                logger.error(f"TARGET_ADDRESS_LEN_REQ region {i}: no ack")
                return False

            if not await self._send_fw_chunks(data, offset, length, label=f"WM region {i}"):
                return False
            offset += length

        # Boot the firmware. The chip answers FW_START_REQ with MCU_EVENT_FW_START
        # (eid=0x01) on EP 0x84, seq-matched to the request, ~15 ms later. That
        # bulk-IN event — not the EP0 FW_N9_RDY control poll, which can go dead at
        # the boot handoff — is the authoritative boot signal.
        option = FW_START_OVERRIDE if override_addr else 0
        logger.debug(f"FW_START_REQ (override=0x{override_addr:x}, option=0x{option:x}); "
                    "awaiting MCU_EVENT_FW_START...")
        resp = await self.transport.send_mcu_command(
            MCU_CMD_FW_START_REQ,
            struct.pack("<II", option, override_addr),
            wait_resp=True, resp_timeout_ms=5000,
        )
        if resp is None:
            logger.error("FW_START_REQ: MCU_EVENT_FW_START never arrived")
            return False
        eid = resp[28] if len(resp) > 28 else None
        logger.debug(f"Firmware booted - MCU_EVENT_FW_START received (eid=0x{eid:02x}).")
        return True

    # ------------------------------------------------------------------
    # Shared chunked sender
    # ------------------------------------------------------------------

    async def _send_fw_chunks(self, blob: bytes, offset: int, length: int, label: str) -> bool:
        sent = 0
        chunk_idx = 0
        while sent < length:
            cur = min(MAX_FW_CHUNK, length - sent)
            chunk = blob[offset + sent: offset + sent + cur]
            if not await self.transport.send_fw_chunk(chunk, timeout_ms=5000):
                logger.error(f"FW_SCATTER failed for {label} at chunk {chunk_idx} (offset {sent}/{length})")
                return False
            sent += cur
            chunk_idx += 1
            await asyncio.sleep(0)
        logger.debug(f"FW_SCATTER {label}: {sent} bytes in {chunk_idx} chunks")
        return True
