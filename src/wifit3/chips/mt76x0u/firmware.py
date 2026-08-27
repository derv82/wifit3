"""MT76x0U firmware upload (M1).

Ports `mt76x0u_load_firmware` + `mt76x02u_mcu_fw_send_data` from
`driver_sources/mt76-source-v6.18/mt76x0/usb_mcu.c` and the surrounding
`mt76x0_chip_onoff` + `mt76x02_wait_for_mac` helpers.

See `MT76X0U.md` for the wire-confirmed 15-step sequence and the
constants verification table.

Per [[feedback_port_full_helper]] every `mt76_wr` in the kernel function
is preserved here. Per [[feedback_port_init_then_start]] the "arm the
radio" writes that live in `start` are NOT in this milestone — M1 ends
at FW_READY ack.
"""
from __future__ import annotations

import logging
import struct
import time
from pathlib import Path
from typing import Callable, Optional

from .constants import (
    CPU_TX_PORT,
    EP_OUT_INBAND_CMD,
    FW_READY_POLL_INTERVAL_MS,
    FW_READY_POLL_TIMEOUT_MS,
    INTER_CHUNK_SLEEP_MS,
    MCU_FW_CHUNK_DATA_MAX,
    MT76X02_FW_HEADER_SIZE,
    MT_CMB_CTRL,
    MT_CMB_CTRL_PLL_LD,
    MT_CMB_CTRL_XTAL_RDY,
    MT_DEV_MODE_FW_RESET,
    MT_DEV_MODE_IVB_TRIGGER,
    MT_FCE_DMA_ADDR,
    MT_FCE_DMA_LEN,
    MT_FCE_PDMA_GLOBAL_CONF,
    MT_FCE_PSE_CTRL,
    MT_FCE_SKIP_FS,
    MT_MAC_CSR0,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_PRE_FW_VALUE,
    MT_MCU_COM_REG0,
    MT_MCU_COM_REG0_FW_READY,
    MT_MCU_DLM_OFFSET,
    MT_MCU_IVB_SIZE,
    MT_MCU_MSG_PORT_SHIFT,
    MT_MCU_MSG_TYPE_CMD,
    MT_TX_CPU_FROM_FCE_BASE_PTR,
    MT_TX_CPU_FROM_FCE_CPU_DESC_IDX,
    MT_TX_CPU_FROM_FCE_MAX_COUNT,
    MT_USB_DMA_CFG,
    MT_USB_DMA_CFG_RX_BULK_AGG_EN,
    MT_USB_DMA_CFG_RX_BULK_AGG_TOUT_MASK,
    MT_USB_DMA_CFG_RX_BULK_EN,
    MT_USB_DMA_CFG_RX_DROP_OR_PAD,
    MT_USB_DMA_CFG_TX_BULK_EN,
    MT_USB_DMA_CFG_UDMA_TX_WL_DROP,
    MT_WLAN_FUN_CTRL,
    MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL,
    MT_WLAN_FUN_CTRL_GPIO_OUT_EN,
    MT_WLAN_FUN_CTRL_WLAN_CLK_EN,
    MT_WLAN_FUN_CTRL_WLAN_EN,
    MT_WLAN_FUN_CTRL_WLAN_RESET,
    MT_WLAN_FUN_CTRL_WLAN_RESET_RF,
    POST_FW_RESET_SLEEP_MS,
)
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[float, str], None]]


# 20µs delay per kernel `udelay(20)` between WLAN_FUN_CTRL writes.
UDELAY_20US = 0.00002

# mt76_poll budget for CMB_CTRL XTAL_RDY+PLL_LD (kernel set_wlan_state:40).
CMB_CTRL_POLL_TIMEOUT_MS = 2000
CMB_CTRL_POLL_INTERVAL_MS = 5

# `mt76x02_wait_for_mac` budget: 500 iterations × 5-10ms = ~3.75 s max.
WAIT_FOR_MAC_MAX_ITERS = 500
WAIT_FOR_MAC_SLEEP_MS = 7.5

# FCE config values, [SRC] mt76x0/usb_mcu.c:136-142
FCE_BASE_PTR_VAL = 0x400230
FCE_MAX_COUNT_VAL = 1
FCE_PDMA_GLOBAL_CONF_VAL = 0x44
FCE_SKIP_FS_VAL = 3
USB_DMA_CFG_AGG_TOUT = 0x20


class FirmwareError(RuntimeError):
    """Bring-up failed somewhere in mt76x0u_load_firmware."""


class FirmwareUploader:
    """Stateful M1 driver: parses a linux-firmware mt7610e.bin and uploads it.

    Usage:
        uploader = FirmwareUploader(transport)
        result = uploader.load_firmware(path_to_mt7610e_bin)
    """

    def __init__(self, transport: MT76x0UTransport,
                 progress_cb: ProgressCb = None):
        self.t = transport
        self.progress_cb = progress_cb

    # ---- Progress reporter ----------------------------------------------
    def _report(self, pct: float, msg: str) -> None:
        logger.info("[%5.1f%%] %s", pct * 100, msg)
        if self.progress_cb is not None:
            self.progress_cb(pct, msg)

    # ---- Sub-steps ------------------------------------------------------
    def _set_wlan_state(self, val: int, enable: bool) -> None:
        """Port of `mt76x0_set_wlan_state`. [SRC] mt76x0/init.c:16-42.

        Sets WLAN_EN | WLAN_CLK_EN if enable, clears WLAN_EN otherwise,
        writes MT_WLAN_FUN_CTRL, then polls MT_CMB_CTRL XTAL_RDY|PLL_LD.
        """
        if enable:
            val |= MT_WLAN_FUN_CTRL_WLAN_EN | MT_WLAN_FUN_CTRL_WLAN_CLK_EN
        else:
            val &= ~MT_WLAN_FUN_CTRL_WLAN_EN
        self.t.write32(MT_WLAN_FUN_CTRL, val)
        time.sleep(UDELAY_20US)

        if enable:
            mask = MT_CMB_CTRL_XTAL_RDY | MT_CMB_CTRL_PLL_LD
            deadline = time.monotonic() + CMB_CTRL_POLL_TIMEOUT_MS / 1000
            while time.monotonic() < deadline:
                if (self.t.read32(MT_CMB_CTRL) & mask) == mask:
                    return
                time.sleep(CMB_CTRL_POLL_INTERVAL_MS / 1000)
            logger.warning("set_wlan_state: CMB_CTRL XTAL_RDY|PLL_LD didn't "
                           "settle within %d ms (kernel only warns; continuing)",
                           CMB_CTRL_POLL_TIMEOUT_MS)

    def chip_onoff(self, enable: bool, reset: bool, label: str = "") -> None:
        """Full port of `mt76x0_chip_onoff(enable, reset)`.

        [SRC] mt76x0/init.c:44-69. Branches on initial WLAN_EN state when
        reset=True. With reset=False, just preserves val and (de)asserts WLAN_EN.

        Per [[feedback_port_all_cases]] all branches are ported here.
        """
        val = self.t.read32(MT_WLAN_FUN_CTRL)
        logger.debug("chip_onoff(enable=%s, reset=%s) %s: initial WLAN_FUN_CTRL=0x%08x  "
                    "(WLAN_EN=%d, WLAN_CLK_EN=%d)",
                    enable, reset, label, val,
                    1 if val & MT_WLAN_FUN_CTRL_WLAN_EN else 0,
                    1 if val & MT_WLAN_FUN_CTRL_WLAN_CLK_EN else 0)

        if reset:
            val |= MT_WLAN_FUN_CTRL_GPIO_OUT_EN
            val &= ~MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL
            if val & MT_WLAN_FUN_CTRL_WLAN_EN:
                logger.debug("chip_onoff: WLAN_EN was set — running reset cycle")
                val_with_reset = val | (
                    MT_WLAN_FUN_CTRL_WLAN_RESET
                    | MT_WLAN_FUN_CTRL_WLAN_RESET_RF
                )
                self.t.write32(MT_WLAN_FUN_CTRL, val_with_reset)
                time.sleep(UDELAY_20US)
                val &= ~(MT_WLAN_FUN_CTRL_WLAN_RESET
                         | MT_WLAN_FUN_CTRL_WLAN_RESET_RF)

        self.t.write32(MT_WLAN_FUN_CTRL, val)
        time.sleep(UDELAY_20US)

        self._set_wlan_state(val, enable=enable)

    def wait_for_mac(self) -> int:
        """`mt76x02_wait_for_mac` — poll MAC_CSR0 until non-zero/non-~0.

        [SRC] mt76x02_mac.h:149-168.
        """
        for _ in range(WAIT_FOR_MAC_MAX_ITERS):
            val = self.t.read32(MT_MAC_CSR0)
            if val not in (0, 0xFFFFFFFF):
                logger.debug("MAC ready: MAC_CSR0=0x%08x", val)
                return val
            time.sleep(WAIT_FOR_MAC_SLEEP_MS / 1000)
        raise FirmwareError("wait_for_mac timed out (MAC_CSR0 stayed 0 / ~0)")

    def firmware_running(self) -> bool:
        """`mt76x0_firmware_running` — BIT(0) of MT_MCU_COM_REG0."""
        return bool(self.t.read32(MT_MCU_COM_REG0) & MT_MCU_COM_REG0_FW_READY)

    def fw_reset(self) -> None:
        """`mt76x02u_mcu_fw_reset` — DEV_MODE wValue=0x1, no payload."""
        self.t.vendor_dev_mode(MT_DEV_MODE_FW_RESET)

    # ---- Per-chunk upload --------------------------------------------
    def _upload_chunk(self, chunk_data: bytes, dst_addr: int) -> None:
        """Mirror of `__mt76x02u_mcu_fw_send_data` for one chunk.

        Steps (4 control transfers + 1 bulk-OUT + 1 RMW):
          1. single_wr(MT_FCE_DMA_ADDR, dst_addr)         (-> 2 ctrl xfers)
          2. single_wr(MT_FCE_DMA_LEN, padded_len << 16)  (-> 2 ctrl xfers)
          3. bulk-OUT EP 0x08: [4B info][padded chunk][4B zero pad]
          4. RMW increment of MT_TX_CPU_FROM_FCE_CPU_DESC_IDX
        """
        # Kernel rounds chunk length up to multiple of 4. Pad with zeros.
        padded_len = (len(chunk_data) + 3) & ~3
        if padded_len != len(chunk_data):
            chunk_data = chunk_data + b"\x00" * (padded_len - len(chunk_data))

        # Info header: PORT|LEN|TYPE_CMD.
        info = (
            (CPU_TX_PORT << MT_MCU_MSG_PORT_SHIFT)
            | MT_MCU_MSG_TYPE_CMD
            | padded_len
        )

        # 4 control transfers via single_wr (each splits into 2).
        self.t.single_wr(MT_FCE_DMA_ADDR, dst_addr)
        self.t.single_wr(MT_FCE_DMA_LEN, padded_len << 16)

        # Bulk-OUT: [info hdr][chunk][zero pad]
        packet = struct.pack("<I", info) + chunk_data + b"\x00\x00\x00\x00"
        self.t.bulk_out(EP_OUT_INBAND_CMD, packet)

        # RMW increment of CPU_DESC_IDX.
        idx = self.t.read32(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX)
        self.t.write32(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX, idx + 1)

    def _upload_section(self, data: bytes, base_addr: int,
                        section_name: str, section_pct_start: float,
                        section_pct_end: float) -> None:
        """Chunk a section and call _upload_chunk for each piece.

        Mirrors `mt76x02u_mcu_fw_send_data` outer loop. Inter-chunk sleep
        per kernel `usleep_range(5000, 10000)`.
        """
        max_len = MCU_FW_CHUNK_DATA_MAX
        offset = 0
        chunk_n = 0
        total = len(data)
        while offset < total:
            this_len = min(max_len, total - offset)
            chunk = data[offset:offset + this_len]
            self._upload_chunk(chunk, base_addr + offset)
            chunk_n += 1
            offset += this_len
            pct = section_pct_start + (
                (offset / total) * (section_pct_end - section_pct_start)
            )
            self._report(pct,
                         f"{section_name} chunk {chunk_n}: {offset}/{total} bytes")
            time.sleep(INTER_CHUNK_SLEEP_MS / 1000)

    def trigger_ivb(self, ivb_body: bytes) -> None:
        """`MT_VEND_DEV_MODE wValue=0x12` with the 64-byte IVB body."""
        if len(ivb_body) != MT_MCU_IVB_SIZE:
            raise FirmwareError(
                f"IVB body must be {MT_MCU_IVB_SIZE} bytes, got {len(ivb_body)}"
            )
        self.t.vendor_dev_mode(MT_DEV_MODE_IVB_TRIGGER, ivb_body)

    def wait_fw_ready(
        self, timeout_ms: int = FW_READY_POLL_TIMEOUT_MS
    ) -> int:
        """`mt76_poll_msec(MT_MCU_COM_REG0, 1, 1, 1000)` — returns poll count.

        Kernel polls every 1 ms for up to 1000 ms. We do the same.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            if self.t.read32(MT_MCU_COM_REG0) & MT_MCU_COM_REG0_FW_READY:
                return polls
            time.sleep(FW_READY_POLL_INTERVAL_MS / 1000)
        raise FirmwareError(
            f"FW_READY timed out after {timeout_ms} ms ({polls} polls)"
        )

    # ---- Header parsing ---------------------------------------------
    @staticmethod
    def parse_fw_header(fw_bytes: bytes) -> dict:
        """Parse the 32-byte `mt76x02_fw_header`. [SRC] mt76x02_mcu.h:71-78."""
        if len(fw_bytes) < MT76X02_FW_HEADER_SIZE:
            raise FirmwareError(
                f"FW too short: {len(fw_bytes)} < {MT76X02_FW_HEADER_SIZE}"
            )
        ilm_len, dlm_len, build_ver, fw_ver = struct.unpack_from(
            "<IIHH", fw_bytes, 0
        )
        build_time = (
            fw_bytes[16:32].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        )
        expected_total = MT76X02_FW_HEADER_SIZE + ilm_len + dlm_len
        if len(fw_bytes) != expected_total:
            raise FirmwareError(
                f"FW size mismatch: got {len(fw_bytes)} bytes, "
                f"header says {expected_total} (ilm={ilm_len}, dlm={dlm_len})"
            )
        if ilm_len <= MT_MCU_IVB_SIZE:
            raise FirmwareError(
                f"FW ilm_len={ilm_len} <= MT_MCU_IVB_SIZE={MT_MCU_IVB_SIZE}"
            )
        return {
            "ilm_len": ilm_len,
            "dlm_len": dlm_len,
            "build_ver": build_ver,
            "fw_ver": fw_ver,
            "build_time": build_time,
            "fw_ver_str": f"{(fw_ver >> 12) & 0xf}.{(fw_ver >> 8) & 0xf}.{fw_ver & 0xff:02d}",
        }

    # ---- The whole M1 sequence --------------------------------------
    def load_firmware(self, fw_file: Path) -> dict:
        """Full mt76x0u_load_firmware port. Returns a result dict.

        Result keys: skipped (bool), polls (int), header (dict).
        Raises FirmwareError on any failure.
        """
        fw_path = Path(fw_file)
        if not fw_path.exists():
            raise FirmwareError(f"FW file not found: {fw_path}")
        fw_bytes = fw_path.read_bytes()
        header = self.parse_fw_header(fw_bytes)
        body = fw_bytes[MT76X02_FW_HEADER_SIZE:]
        ivb = body[:MT_MCU_IVB_SIZE]
        ilm_remainder = body[MT_MCU_IVB_SIZE:header["ilm_len"]]
        dlm = body[header["ilm_len"]:header["ilm_len"] + header["dlm_len"]]

        self._report(0.00, f"FW: ver {header['fw_ver_str']}  "
                            f"build 0x{header['build_ver']:04x}  "
                            f"time {header['build_time']!r}  "
                            f"(ilm={header['ilm_len']} dlm={header['dlm_len']})")

        # ---- Step 0z: WARM chip pre-reset.
        # If WLAN_EN is set at entry, the chip has FW running from a
        # previous session. We MUST do the WLAN_RESET hardware reset BEFORE
        # the pre-clean clears WLAN_EN, because chip_onoff(reset=True)'s
        # reset cycle is gated on `if val & WLAN_EN` — once pre-clean
        # clears WLAN_EN, the bring-up's reset cycle skips, FW survives,
        # fw_reset (vendor cmd) only partially kills it, and the
        # subsequent bulk-OUT FW upload truncates mid-chunk.
        #
        # This block does the cycle directly:
        #   - assert GPIO_OUT_EN + WLAN_RESET + WLAN_RESET_RF (with WLAN_EN
        #     still set), write
        #   - sleep
        #   - clear the RESET bits, write
        #   - then fall through to the normal pre-clean → bring-up flow,
        #     which will see WLAN_EN=0 (because we kept it set + then
        #     pre-clean clears it) and do a clean cold-boot upload.
        initial_val = self.t.read32(MT_WLAN_FUN_CTRL)
        # The WLAN_RESET cycle below is what actually wakes RX — it must fire on ANY *dirty* chip,
        # not only WLAN_EN=1. A replug that doesn't power-cycle the dongle (USB passthrough / a VM)
        # leaves FW resident with WLAN_EN=0: the chip is dirty but the old WLAN_EN-only gate missed
        # it, so RX stayed dead on the first cold boot while every warm boot worked. So gate on
        # firmware_running() too, and force WLAN_EN|WLAN_CLK_EN during the reset so it bites from the
        # WLAN_EN=0 state (mirrors the working warm path). [EXPERIMENT — cold-first-boot no-RX]
        fw_resident = self.firmware_running()
        if (initial_val & MT_WLAN_FUN_CTRL_WLAN_EN) or fw_resident:
            self._report(0.01,
                f"Dirty chip (WLAN_FUN_CTRL=0x{initial_val:08x}, fw_running={fw_resident}) — "
                f"forcing WLAN_RESET hardware cycle")
            val = initial_val
            val |= (MT_WLAN_FUN_CTRL_GPIO_OUT_EN
                    | MT_WLAN_FUN_CTRL_WLAN_EN
                    | MT_WLAN_FUN_CTRL_WLAN_CLK_EN)
            val &= ~MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL
            val |= (MT_WLAN_FUN_CTRL_WLAN_RESET
                    | MT_WLAN_FUN_CTRL_WLAN_RESET_RF)
            self.t.write32(MT_WLAN_FUN_CTRL, val)
            time.sleep(UDELAY_20US)
            val &= ~(MT_WLAN_FUN_CTRL_WLAN_RESET
                     | MT_WLAN_FUN_CTRL_WLAN_RESET_RF)
            self.t.write32(MT_WLAN_FUN_CTRL, val)
            # Settle: chip's MCU is mid-reset; brief sleep so subsequent
            # vendor writes don't time-out hitting a wedged USB pipe.
            time.sleep(0.020)   # 20 ms

        # ---- Step 0a: chip OFF cycle. [SRC] mt76x0/usb.c:259
        # Kernel probe explicitly does this with the comment
        # "Disable the HW, otherwise MCU fail to initialize on hot reboot".
        # Critical on Windows where the chip may be in a warm state from a
        # prior session — without this, the first vendor write after the
        # subsequent fw_reset times out.
        self._report(0.02, "chip_onoff(enable=false, reset=false) — pre-clean")
        self.chip_onoff(enable=False, reset=False, label="pre-clean")
        self._report(0.03, "wait_for_mac (after pre-clean)")
        self.wait_for_mac()

        # ---- Step 1: chip on + reset
        self._report(0.04, "chip_onoff(enable=true, reset=true)")
        self.chip_onoff(enable=True, reset=True, label="bring-up")

        # ---- Step 2: wait for MAC
        self._report(0.05, "wait_for_mac (after bring-up)")
        self.wait_for_mac()

        # ---- Step 3: initial DMA cfg = RX_BULK_EN | TX_BULK_EN
        self.t.write32(MT_USB_DMA_CFG,
                       MT_USB_DMA_CFG_RX_BULK_EN | MT_USB_DMA_CFG_TX_BULK_EN)

        # ---- Step 4: warm-boot detection (informational only)
        # Past iterations short-circuited here ("FW already running — skip
        # upload"). That fails: the pre-clean cleared WLAN_EN, so the
        # subsequent chip_onoff(reset=True) skipped its conditional reset
        # cycle (kernel: `if (val & WLAN_EN) { assert RESET; deassert; }`).
        # WLAN_EN was just toggled, leaving the WLAN clock restarted but
        # the MCU command channel in stale state from the previous session.
        # Result: FW_READY bit stays set, but MCU bulk-IN times out on
        # every command.
        #
        # The kernel itself has no such short-circuit — it always runs the
        # full upload. The `fw_reset` we issue a few steps down (DEV_MODE
        # wValue=0x1) kills any running FW first, so re-upload is safe.
        # Cost: ~700 ms FW upload per process start. Worth it for warm-reattach
        # reliability — no more "please replug" cycles between test runs.
        was_warm = self.firmware_running()
        if was_warm:
            self._report(0.06, "FW already running — will force-reset and re-upload")

        # ---- Step 5: MAC_SYS_CTRL = 0x2c (kernel does this magic write)
        self.t.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_PRE_FW_VALUE)

        # ---- Step 6: DMA cfg with AGG_TOUT field set
        val = self.t.read32(MT_USB_DMA_CFG)
        val |= (
            MT_USB_DMA_CFG_RX_BULK_EN
            | MT_USB_DMA_CFG_TX_BULK_EN
            | (USB_DMA_CFG_AGG_TOUT & MT_USB_DMA_CFG_RX_BULK_AGG_TOUT_MASK)
        )
        self.t.write32(MT_USB_DMA_CFG, val)

        # ---- Step 7-8: FW reset + 5 ms sleep. The chip's MCU is briefly
        # unresponsive after this (~10-50ms); the kernel handles that via
        # __mt76u_vendor_request's 10× retry loop. Our transport.py mirrors
        # that retry loop, so the next write transparently retries until the
        # chip recovers.
        self._report(0.08, "fw_reset (DEV_MODE wValue=0x1)")
        self.fw_reset()
        time.sleep(POST_FW_RESET_SLEEP_MS / 1000)

        # ---- Step 9: PSE_CTRL = 1 (TWICE per WIRE). Port verbatim;
        # the duplicate isn't in kernel source but appears on the bus.
        self.t.write32(MT_FCE_PSE_CTRL, 1)
        self.t.write32(MT_FCE_PSE_CTRL, 1)

        # ---- Step 10: FCE config (4 writes)
        self.t.write32(MT_TX_CPU_FROM_FCE_BASE_PTR, FCE_BASE_PTR_VAL)
        self.t.write32(MT_TX_CPU_FROM_FCE_MAX_COUNT, FCE_MAX_COUNT_VAL)
        self.t.write32(MT_FCE_PDMA_GLOBAL_CONF, FCE_PDMA_GLOBAL_CONF_VAL)
        self.t.write32(MT_FCE_SKIP_FS, FCE_SKIP_FS_VAL)

        # ---- Step 11: UDMA_TX_WL_DROP set then clear
        val = self.t.read32(MT_USB_DMA_CFG)
        self.t.write32(MT_USB_DMA_CFG, val | MT_USB_DMA_CFG_UDMA_TX_WL_DROP)
        self.t.write32(MT_USB_DMA_CFG, val & ~MT_USB_DMA_CFG_UDMA_TX_WL_DROP)

        # ---- Step 12: Upload ILM remainder @ 0x40, then DLM @ 0x80000
        self._report(0.10, "upload ILM remainder")
        self._upload_section(
            ilm_remainder,
            base_addr=MT_MCU_IVB_SIZE,
            section_name="ILM",
            section_pct_start=0.10,
            section_pct_end=0.85,
        )
        self._report(0.85, "upload DLM")
        self._upload_section(
            dlm,
            base_addr=MT_MCU_DLM_OFFSET,
            section_name="DLM",
            section_pct_start=0.85,
            section_pct_end=0.95,
        )

        # ---- Step 13: IVB trigger (the 64 bytes that bootstrap the FW)
        self._report(0.96, "trigger IVB (DEV_MODE wValue=0x12, 64B payload)")
        self.trigger_ivb(ivb)

        # ---- Step 14: poll FW_READY
        self._report(0.98, "poll MT_MCU_COM_REG0 for FW_READY")
        polls = self.wait_fw_ready()
        self._report(0.99, f"FW_READY ack after {polls} poll(s)")

        # ---- Step 15: post-upload PSE_CTRL = 1
        self.t.write32(MT_FCE_PSE_CTRL, 1)

        self._report(1.00, "FW upload complete")
        return {"skipped": False, "polls": polls, "header": header,
                "was_warm": was_warm}

    # ------------------------------------------------------------------
    # Post-FW init (steps run by mt76x0u_init_hardware AFTER mcu_init).
    # These are M2 prerequisites — without them the MCU response path is
    # not armed and MCU bulk-IN reads on EP 0x85 time out.
    # ------------------------------------------------------------------
    def init_usb_dma(self) -> None:
        """`mt76x0_init_usb_dma` — [SRC] mt76x0/usb.c:46-71. [WIRE] f401-411.

        1. read DMA_CFG, set RX_BULK_EN|TX_BULK_EN, clear RX_BULK_AGG_EN, write.
        2. read MT_MCU_COM_REG0 (MCU-ready warning only — kernel doesn't fail).
        3. RX_DROP_OR_PAD set then clear (toggle).
        """
        val = self.t.read32(MT_USB_DMA_CFG)
        val |= MT_USB_DMA_CFG_RX_BULK_EN | MT_USB_DMA_CFG_TX_BULK_EN
        val &= ~MT_USB_DMA_CFG_RX_BULK_AGG_EN
        self.t.write32(MT_USB_DMA_CFG, val)

        mcu_status = self.t.read32(MT_MCU_COM_REG0)
        if not (mcu_status & MT_MCU_COM_REG0_FW_READY):
            logger.warning("init_usb_dma: MT_MCU_COM_REG0 FW_READY bit cleared "
                           "(0x%08x) — kernel only warns; continuing", mcu_status)

        val = self.t.read32(MT_USB_DMA_CFG)
        self.t.write32(MT_USB_DMA_CFG, val | MT_USB_DMA_CFG_RX_DROP_OR_PAD)
        self.t.write32(MT_USB_DMA_CFG, val & ~MT_USB_DMA_CFG_RX_DROP_OR_PAD)

    def reset_csr_bbp(self) -> None:
        """`mt76x0_reset_csr_bbp` — [SRC] mt76x0/init.c:72-81. [WIRE] f417-421.

        Writes MAC_SYS_CTRL = RESET_CSR | RESET_BBP, sleeps 200 ms, then
        clears those bits via RMW. This is the MAC reset cycle that arms
        the MCU response DMA — MCU bulk-IN reads stall until this runs.
        """
        from .constants import (
            MT_MAC_SYS_CTRL_RESET_BBP,
            MT_MAC_SYS_CTRL_RESET_CSR,
        )
        reset_mask = MT_MAC_SYS_CTRL_RESET_CSR | MT_MAC_SYS_CTRL_RESET_BBP
        self.t.write32(MT_MAC_SYS_CTRL, reset_mask)
        time.sleep(0.200)   # kernel: msleep(200)
        cur = self.t.read32(MT_MAC_SYS_CTRL)
        self.t.write32(MT_MAC_SYS_CTRL, cur & ~reset_mask)
