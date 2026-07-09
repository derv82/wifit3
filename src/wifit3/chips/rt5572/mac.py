"""rt2800usb MAC-layer helpers: chip probe + warm detection + USB-side
bootstrap.

The big rt2800_init_registers (~600 lines of MAC config) lands in
M2b-2 — this module currently just covers the USB-side bootstrap step
that the kernel runs first as ``rt2800usb_init_registers``.
"""
from __future__ import annotations

import logging
import time

from dataclasses import dataclass

from .constants import (
    GPIO_CTRL,
    GPIO_CTRL_DIR2,
    MAC_ADDR_DW0,
    MAC_ADDR_DW1,
    MAC_CSR0,
    MAC_CSR0_CHIPSET_MASK,
    MAC_CSR0_CHIPSET_SHIFT,
    MAC_CSR0_REVISION_MASK,
    MAC_SYS_CTRL,
    MAC_SYS_CTRL_RESET_BBP,
    MAC_SYS_CTRL_RESET_CSR,
    PBF_SYS_CTRL,
    PBF_SYS_CTRL_READY,
    REGISTER_TIMEOUT_MS,
    RT_NAMES,
    RT_SUPPORTED,
    USB_DEVICE_MODE,
    USB_MODE_RESET,
    USB_VENDOR_REQUEST_OUT,
)
from .transport import RT5572Transport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChipId:
    """Result of reading MAC_CSR0 + decoding the silicon ID."""
    raw: int                # full MAC_CSR0 word
    silicon_id: int         # [31:16] = chipset family (RT5390 / RT5592 / RT3572 / ...)
    revision: int           # [15:0]  = revision (e.g. 0x0223 = REV_RT5592C)
    name: str               # human-readable, e.g. "RT5390" or "0x4567"
    is_supported: bool


def read_chip_id(t: RT5572Transport) -> ChipId:
    """Read MAC_CSR0 and decode the kernel-style chipset + revision.

    Mirrors ``rt2800_probe_rt`` (rt2800lib.c:11987-12031).
    """
    reg = t.read32(MAC_CSR0)
    silicon = (reg & MAC_CSR0_CHIPSET_MASK) >> MAC_CSR0_CHIPSET_SHIFT
    revision = reg & MAC_CSR0_REVISION_MASK
    name = RT_NAMES.get(silicon, f"0x{silicon:04x}")
    return ChipId(
        raw=reg,
        silicon_id=silicon,
        revision=revision,
        name=name,
        is_supported=silicon in RT_SUPPORTED,
    )


def read_perm_mac(t: RT5572Transport) -> bytes:
    """Read 6 permanent MAC bytes from MAC_ADDR_DW0/DW1.

    Kernel pulls these from EEPROM at probe time and writes them into
    MAC_ADDR_DW0/DW1 — so on a *warm* chip you get the real MAC. On a
    truly cold chip these may be zeros until M2 runs init_registers.
    """
    dw0 = t.read32(MAC_ADDR_DW0)
    dw1 = t.read32(MAC_ADDR_DW1)
    return bytes((
        dw0 & 0xFF,
        (dw0 >> 8) & 0xFF,
        (dw0 >> 16) & 0xFF,
        (dw0 >> 24) & 0xFF,
        dw1 & 0xFF,
        (dw1 >> 8) & 0xFF,
    ))


def probe_hw_gpio(t: RT5572Transport) -> None:
    """Enable rfkill polling: set the rfkill-switch GPIO pin to input direction
    (GPIO_CTRL_DIR2=1). Kernel does this in rt2800_probe_hw, right after
    init_eeprom and before probe_hw_mode (the xtal read). [SRC] rt2800lib.c
    rt2800_probe_hw."""
    reg = t.read32(GPIO_CTRL)
    reg |= GPIO_CTRL_DIR2
    t.write32(GPIO_CTRL, reg & 0xFFFFFFFF)


def set_radio_led(t: RT5572Transport, led_mcu_reg: int, enabled: bool = True) -> None:
    """Set the radio LED via MCU_LED — the USB path of rt2800_brightness_set for
    LED_TYPE_RADIO: mcu_request(MCU_LED, 0xff, ledmode, enabled ? 0x20 : 0), where
    ledmode is the EEPROM_FREQ LED_MODE field. ``led_mcu_reg`` is the raw EEPROM FREQ
    word (the kernel's led_mcu_reg). rt2x00leds turns the radio LED on during
    rt2x00lib_enable_radio. [SRC] rt2800lib.c:1646-1648, rt2x00leds.c:85."""
    from .constants import EEPROM_FREQ_LED_MODE, MCU_LED
    from .firmware import mcu_request
    ledmode = (led_mcu_reg & EEPROM_FREQ_LED_MODE) >> 8
    mcu_request(t, MCU_LED, token=0xFF, arg0=ledmode, arg1=0x20 if enabled else 0)


def is_chip_warm(t: RT5572Transport) -> bool:
    """True iff a prior session left the chip fully initialized.

    Heuristic verified [WIRE M1]: on a freshly-plugged dongle
    ``PBF_SYS_CTRL`` reads ``0x00002080`` — bit 13 (0x2000) is set as
    a "needs init" marker, bit 7 (READY) is also set. Kernel
    ``rt2800usb_init_registers`` (rt2800usb.c:280-281) explicitly
    clears bit 13 as part of init. So:

        cold = bit 13 set      (pre-init state)
        warm = bit 13 cleared + bit 7 set (post-init, FW running)
    """
    try:
        pbf_ctrl = t.read32(PBF_SYS_CTRL)
    except Exception as e:
        logger.debug("warm probe failed: %s", e)
        return False
    # The "pre-init" marker that init_registers clears.
    pre_init_bit = 1 << 13
    return bool(pbf_ctrl & PBF_SYS_CTRL_READY) and not (pbf_ctrl & pre_init_bit)


# ----------------------------------------------------------------------
# rt2800usb_init_registers — first thing to run post-FW upload.
# Port of rt2800usb.c:270-294.
# ----------------------------------------------------------------------
PBF_SYS_CTRL_PRE_INIT = 1 << 13   # bit kernel clears in init_registers


def usb_init_registers(t: RT5572Transport) -> None:
    """USB-side bootstrap that runs after FW upload.

    Kernel sequence (rt2800usb.c:270-294):

        wait_csr_ready                          MAC_CSR0 non-zero
        PBF_SYS_CTRL &= ~0x00002000             clear pre-init bit 13
        MAC_SYS_CTRL = RESET_CSR | RESET_BBP    reset MAC + BBP cores
        vendor_request(USB_DEVICE_MODE, 0,      reset USB endpoints
                       USB_MODE_RESET)
        MAC_SYS_CTRL = 0                        release MAC reset

    After this returns, ``is_chip_warm`` should report True (PBF bit 13
    cleared, READY still set).  M2b-2 will follow up with the bulk
    rt2800_init_registers MAC configuration.
    """
    # Brief wait for MAC_CSR0 to be readable (already true post-FW boot,
    # but kernel is paranoid).
    for _ in range(100):
        reg = t.read32(MAC_CSR0)
        if reg and reg != 0xFFFFFFFF:
            break
        time.sleep(0.001)
    else:
        raise IOError("usb_init_registers: MAC_CSR0 never came up")

    # Clear pre-init bit 13.
    pbf = t.read32(PBF_SYS_CTRL)
    t.write32(PBF_SYS_CTRL, pbf & ~PBF_SYS_CTRL_PRE_INIT & 0xFFFFFFFF)

    # Reset MAC + BBP cores via MAC_SYS_CTRL.
    t.write32(MAC_SYS_CTRL, MAC_SYS_CTRL_RESET_CSR | MAC_SYS_CTRL_RESET_BBP)

    # USB endpoint reset (vendor request: bRequest=USB_DEVICE_MODE,
    # wValue=USB_MODE_RESET, wIndex=0).
    t.dev.ctrl_transfer(
        USB_VENDOR_REQUEST_OUT,
        USB_DEVICE_MODE,
        USB_MODE_RESET,
        0,
        b"",
        REGISTER_TIMEOUT_MS,
    )

    # Release MAC reset.
    t.write32(MAC_SYS_CTRL, 0)


def config_filter(t: RT5572Transport, filter_flags: int = 0,
                  monitoring: bool = False) -> None:
    """rt2800_config_filter — set the RX_FILTER_CFG DROP_* fields from the mac80211
    filter flags. VER errors are always dropped and broadcast always accepted (no
    filter for it), per the kernel comment. init_registers calls this with FIF_ALLMULTI
    and monitoring off (→ 0x1bf97 on this card); monitor-enable re-runs it with
    monitoring on. [SRC] rt2800lib.c rt2800_config_filter."""
    from . import constants as C
    reg = t.read32(C.RX_FILTER_CFG)

    def drop(mask: int, cond: bool) -> None:
        nonlocal reg
        reg = (reg | mask) if cond else (reg & ~mask & 0xFFFFFFFF)

    ctrl = bool(filter_flags & C.FIF_CONTROL)
    drop(C.RX_FILTER_CFG_DROP_CRC_ERROR, not (filter_flags & C.FIF_FCSFAIL))
    drop(C.RX_FILTER_CFG_DROP_PHY_ERROR, not (filter_flags & C.FIF_PLCPFAIL))
    drop(C.RX_FILTER_CFG_DROP_NOT_TO_ME, not monitoring)
    drop(C.RX_FILTER_CFG_DROP_NOT_MY_BSSD, False)
    drop(C.RX_FILTER_CFG_DROP_VER_ERROR, True)
    drop(C.RX_FILTER_CFG_DROP_MULTICAST, not (filter_flags & C.FIF_ALLMULTI))
    drop(C.RX_FILTER_CFG_DROP_BROADCAST, False)
    drop(C.RX_FILTER_CFG_DROP_DUPLICATE, True)
    drop(C.RX_FILTER_CFG_DROP_CF_END_ACK, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_CF_END, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_ACK, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_CTS, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_RTS, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_PSPOLL, not (filter_flags & C.FIF_PSPOLL))
    drop(C.RX_FILTER_CFG_DROP_BA, False)
    drop(C.RX_FILTER_CFG_DROP_BAR, not ctrl)
    drop(C.RX_FILTER_CFG_DROP_CNTL, not ctrl)
    t.write32(C.RX_FILTER_CFG, reg)


def toggle_rx(t: RT5572Transport, enable: bool) -> None:
    """rt2800usb_start_queue / stop_queue for QID_RX — a MAC_SYS_CTRL RMW of
    just ENABLE_RX. mac80211 brackets every operational reconfigure (filter,
    antenna, channel) with an RX off before and on after, so the changes latch
    while the receiver is quiescent. [SRC] rt2800usb.c:46-92."""
    from . import constants as C
    reg = t.read32(C.MAC_SYS_CTRL)
    if enable:
        reg |= C.MAC_SYS_CTRL_ENABLE_RX
    else:
        reg = reg & ~C.MAC_SYS_CTRL_ENABLE_RX & 0xFFFFFFFF
    t.write32(C.MAC_SYS_CTRL, reg)


def config_retry_limit(t: RT5572Transport, short_retry: int = 7,
                       long_retry: int = 4) -> None:
    """rt2800_config_retry_limit — TX_RTY_CFG SHORT/LONG retry limits from the
    mac80211 conf. Defaults are mac80211's hw retry counts (short 7 / long 4),
    which is what the capture writes. [SRC] rt2800lib.c:5636."""
    from . import constants as C
    reg = t.read32(C.TX_RTY_CFG)
    reg = (reg & ~C.TX_RTY_CFG_SHORT_RTY_LIMIT & 0xFFFFFFFF) | (short_retry & 0xFF)
    reg = (reg & ~C.TX_RTY_CFG_LONG_RTY_LIMIT & 0xFFFFFFFF) | ((long_retry & 0xFF) << 8)
    t.write32(C.TX_RTY_CFG, reg)


def config_ps_awake(t: RT5572Transport) -> None:
    """rt2800_config_ps for STATE_AWAKE — the only power state a monitor
    interface visits. Clears the AUTOWAKEUP_CFG auto-sleep fields, then
    set_device_state(AWAKE) = mcu_request(MCU_WAKEUP, 0xff, 0, 2) (the same
    MCU wake the cold radio-on issues). [SRC] rt2800lib.c:5649 (else branch) +
    rt2800usb.c:325-333."""
    from . import constants as C
    from .firmware import mcu_request
    reg = t.read32(C.AUTOWAKEUP_CFG)
    reg = reg & ~(C.AUTOWAKEUP_CFG_AUTO_LEAD_TIME | C.AUTOWAKEUP_CFG_TBCN_BEFORE_WAKE
                  | C.AUTOWAKEUP_CFG_AUTOWAKE) & 0xFFFFFFFF
    t.write32(C.AUTOWAKEUP_CFG, reg)
    mcu_request(t, C.MCU_WAKEUP, token=0xFF, arg0=0, arg1=2)


def update_survey(t: RT5572Transport) -> None:
    """rt2800_update_survey — read the three channel-activity counters to bank
    the current channel's survey before a channel change (the counters are
    read-to-accumulate in the kernel; here the reads just replay). [SRC]
    rt2800lib.c:1255."""
    from . import constants as C
    t.read32(C.CH_IDLE_STA)
    t.read32(C.CH_BUSY_STA)
    t.read32(C.CH_BUSY_STA_SEC)


# ----------------------------------------------------------------------
# rt2800_enable_radio + rt2800usb_enable_radio — turn RX/TX on at the
# MAC + WPDMA + USB-DMA level. Without this, the chip is fully
# initialized but bulk-IN delivers nothing.
#
# [SRC] rt2800lib.c:10790-10860 (rt2800_enable_radio)
#       rt2800usb.c:296-318 (rt2800usb_enable_radio)
# ----------------------------------------------------------------------
def write_mac_address(t: RT5572Transport, mac: bytes, u2me_mask: int = 0x00) -> None:
    """Program the chip's self-MAC into MAC_ADDR_DW0/DW1.

    Without this set to a real value, RX may stay silent on some chip
    revs because the MAC matching engine has no valid identity.

    UNICAST_TO_ME_MASK lives in MAC_ADDR_DW1 bits [23:16] and gates the
    "unicast to me" match the autoresponder ACKs on. Kernel default (STA
    mode) is 0xFF — strict-match. ``u2me_mask`` defaults to 0 because wifit3
    is always-monitor: PMKID/SAE/handshake-capture all rely on receiving
    unicast DATA addressed to a *forged* source MAC the chip doesn't
    recognise as itself. Active-monitor (WPS/EAP) passes 0xFF so the
    autoresponder strict-matches and HW-ACKs frames to our forged MAC.

    mt76 analog: chips/mt76x0u/driver.py rewrites MT_MAC_ADDR_DW1 with
    U2ME_MASK cleared for the same reason.

    Realtek analog: chips/rtl8xxxu / rtw88 set RCR with ACCEPT_DATA +
    ACCEPT_AP, which similarly opens unicast-not-to-me on those families.
    """
    if len(mac) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
    dw0 = mac[0] | (mac[1] << 8) | (mac[2] << 16) | (mac[3] << 24)
    dw1 = (mac[5] << 8) | mac[4] | ((u2me_mask & 0xFF) << 16)
    t.write32(0x1008, dw0)   # MAC_ADDR_DW0
    t.write32(0x100C, dw1)   # MAC_ADDR_DW1


def _wait_wpdma_ready(t: RT5572Transport) -> bool:
    """rt2800_wait_wpdma_ready (rt2800lib.c:566-587) — poll
    WPDMA_GLO_CFG until TX_DMA_BUSY and RX_DMA_BUSY are both clear."""
    from .constants import (
        REGISTER_BUSY_COUNT,
        WPDMA_GLO_CFG,
        WPDMA_GLO_CFG_RX_DMA_BUSY,
        WPDMA_GLO_CFG_TX_DMA_BUSY,
    )
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(WPDMA_GLO_CFG)
        if not (reg & (WPDMA_GLO_CFG_TX_DMA_BUSY | WPDMA_GLO_CFG_RX_DMA_BUSY)):
            return True
        time.sleep(0.010)
    return False


def usb_enable_radio_dma(t: RT5572Transport) -> None:
    """rt2800usb_enable_radio: wait for WPDMA idle, then enable bulk RX/TX DMA via
    USB_DMA_CFG — RX bulk-agg limit ((rx_limit*DATA_FRAME_SIZE/1024)-3, masked to 8 bits)
    + agg timeout 128 + RX/TX bulk enable. [SRC] rt2800usb.c:296-318."""
    from .constants import (
        DATA_FRAME_SIZE, RX_QUEUE_LIMIT, USB_DMA_CFG, USB_DMA_CFG_PHY_CLEAR,
        USB_DMA_CFG_RX_BULK_AGG_EN, USB_DMA_CFG_RX_BULK_AGG_LIMIT_MASK,
        USB_DMA_CFG_RX_BULK_AGG_TIMEOUT_MASK, USB_DMA_CFG_RX_BULK_EN, USB_DMA_CFG_TX_BULK_EN,
    )
    if not _wait_wpdma_ready(t):
        raise IOError("WPDMA never reported idle — chip wedged")
    reg = 0
    reg &= ~USB_DMA_CFG_PHY_CLEAR
    reg &= ~USB_DMA_CFG_RX_BULK_AGG_EN
    reg |= 128 & USB_DMA_CFG_RX_BULK_AGG_TIMEOUT_MASK
    agg_limit = (RX_QUEUE_LIMIT * DATA_FRAME_SIZE // 1024) - 3
    reg |= (agg_limit << 8) & USB_DMA_CFG_RX_BULK_AGG_LIMIT_MASK
    reg |= USB_DMA_CFG_RX_BULK_EN
    reg |= USB_DMA_CFG_TX_BULK_EN
    t.write32(USB_DMA_CFG, reg)


def enable_radio_finish(t: RT5572Transport, ev) -> None:
    """Tail of rt2800_enable_radio: MAC_SYS_CTRL TX-only → WPDMA TX/RX DMA →
    MAC_SYS_CTRL TX+RX (all three are RMW), then the EEPROM LED_AG/ACT/POLARITY MCU
    commands and a final radio-LED-on (rt2x00leds_led_radio). [SRC] rt2800lib.c
    rt2800_enable_radio tail."""
    from . import constants as C
    from .eeprom import (
        EEPROM_OFFSET_FREQ, EEPROM_OFFSET_LED_ACT_CONF, EEPROM_OFFSET_LED_AG_CONF,
        EEPROM_OFFSET_LED_POLARITY,
    )
    from .firmware import mcu_request

    reg = t.read32(C.MAC_SYS_CTRL)
    reg = (reg | C.MAC_SYS_CTRL_ENABLE_TX) & ~C.MAC_SYS_CTRL_ENABLE_RX & 0xFFFFFFFF
    t.write32(C.MAC_SYS_CTRL, reg)
    time.sleep(0.000_05)

    reg = t.read32(C.WPDMA_GLO_CFG)
    reg |= (C.WPDMA_GLO_CFG_ENABLE_TX_DMA | C.WPDMA_GLO_CFG_ENABLE_RX_DMA
            | C.WPDMA_GLO_CFG_TX_WRITEBACK_DONE)
    t.write32(C.WPDMA_GLO_CFG, reg & 0xFFFFFFFF)

    reg = t.read32(C.MAC_SYS_CTRL)
    reg |= C.MAC_SYS_CTRL_ENABLE_TX | C.MAC_SYS_CTRL_ENABLE_RX
    t.write32(C.MAC_SYS_CTRL, reg)

    for cmd, off in ((C.MCU_LED_AG_CONF, EEPROM_OFFSET_LED_AG_CONF),
                     (C.MCU_LED_ACT_CONF, EEPROM_OFFSET_LED_ACT_CONF),
                     (C.MCU_LED_LED_POLARITY, EEPROM_OFFSET_LED_POLARITY)):
        w = ev.word(off)
        mcu_request(t, cmd, token=0xFF, arg0=w & 0xFF, arg1=(w >> 8) & 0xFF)

    set_radio_led(t, ev.word(EEPROM_OFFSET_FREQ))


def enable_radio(t: RT5572Transport, silicon_id: int = 0, ev=None) -> None:
    """Enable RX + TX on the radio.  Call AFTER all init_* steps.

    Port of rt2800usb_enable_radio (rt2800usb.c:296-318) +
    rt2800_enable_radio (rt2800lib.c:10790-10860) minus the LED MCU
    setup (needs EEPROM).

    ``silicon_id`` is used to trigger the RT3070/RT3071/RT3572 USB-only
    MCU_CURRENT request that the kernel makes between init_rfcsr and
    MAC_SYS_CTRL enable. Default 0 = skip (back-compat for RT5392 path).
    """
    from .constants import MCU_CURRENT, RT_RT3070, RT_RT3071, RT_RT3572

    # 1) rt2800usb_enable_radio: wait for WPDMA idle, then turn on USB_DMA_CFG with
    # the RX bulk-agg limit + bulk-IN/OUT enable bits.  [SRC] rt2800usb.c:296-318
    usb_enable_radio_dma(t)

    # 2) rt2800_enable_radio body. Kernel calls init_registers/init_bbp/
    # init_rfcsr inside this — we already ran those.
    if not _wait_wpdma_ready(t):
        raise IOError("WPDMA never reported idle (second wait)")

    # 2a) RT3070/3071/3572 USB-only MCU_CURRENT request.
    # [SRC] rt2800lib.c:10829-10836. The kernel sleeps 200µs before and
    # 10µs after. Without this, RX-side calibration registers stay in
    # a half-applied state — symptom: chip detects energy (false CCAs
    # in RX_STA_CNT1 increment) but all decoded frames come back with
    # CRC errors, so DROP_CRC_ERR in RX_FILTER_CFG blocks every URB.
    if silicon_id in (RT_RT3070, RT_RT3071, RT_RT3572):
        from .firmware import mcu_request
        time.sleep(0.000_2)         # udelay(200)
        mcu_request(t, MCU_CURRENT, token=0, arg0=0, arg1=0)
        time.sleep(0.000_01)        # udelay(10)
        logger.debug("MCU_CURRENT sent for RT3070/3071/3572-family chip")

    # 3) MCU_BOOT_SIGNAL + H2M setup — MOVED to bbp.prepare_bbp which
    # runs BETWEEN init_registers and init_bbp. Kernel does this
    # ordering inside its rt2800_enable_radio. Without prepare_bbp
    # being called earlier, init_bbp/init_rfcsr writes appear OK but
    # the BBP→RF chain never commits and bulk-IN stays silent.

    # 4-7) MAC/WPDMA enable + the EEPROM-derived LED MCU commands — the tail of
    # rt2800_enable_radio, extracted so the cold-walk verifier can drive it in
    # isolation (the live-monitor RX_FILTER below is NOT part of the kernel's
    # enable_radio — that is applied later by airmon).
    if ev is not None:
        enable_radio_finish(t, ev)

    # 8) Open RX_FILTER_CFG for monitor mode. Default state (after
    # init_registers / chip reset) drops everything not addressed to
    # our MAC, which blackholes all beacons / broadcasts / data we
    # want to capture. Kernel `rt2800_config_filter` with monitor
    # flags clears DROP_NOT_TO_ME among others.
    #
    # Same lesson as [[feedback_station_vs_monitor_rcr]] on RTL8187L
    # M7 — chip default is *station* mode; monitor capture needs an
    # explicit "accept everything" pass.
    #
    # Keep only DROP_CRC_ERROR + DROP_VER_ERROR (real packet errors,
    # not address filtering). Everything else cleared.
    monitor_rx_filter = 0x00000011   # CRC_ERROR | VER_ERROR
    t.write32(0x1400, monitor_rx_filter)  # RX_FILTER_CFG

    # 9) MAC address programming — moved out of enable_radio. Callers
    # must call write_mac_address(t, mac_bytes) explicitly after
    # reading EEPROM (see eeprom.parse_eeprom).
