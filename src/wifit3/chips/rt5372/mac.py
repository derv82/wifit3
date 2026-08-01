"""MAC-level bring-up for the RT5372 (RT5392): chip probe, radio enable, register init.

Ported from ``rt2800lib.c`` (chip logic) + ``rt2800usb.c`` (USB glue), confirmed
against captures_rt2800usb_rt5372/capture-1 by ``scripts/verify_pcap.py rt5372``.

Scope note: the kernel's ``rt2800_init_registers`` is a giant per-chip switch. We
port the RT5390/RT5392 (RF53xx) path; the only chip-gated divergence from the family
baseline is the TX_SW_CFG arm. Unrelated chip families are out of scope — this driver
claims only 148f:5372 — so their switch arms are intentionally not transcribed.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo, get_field, set_field
from .eeprom import EepromValues
from .transport import RT5372Transport


def probe_rt(t: RT5372Transport) -> ChipInfo:
    """Read chip id + revision from MAC_CSR0 [SRC rt2800lib.c:11987-12031
    rt2800_probe_rt]. PAU05/PAU06 report rt=0x5392 (RT5392)."""
    reg = t.register_read(C.MAC_CSR0)
    return ChipInfo(rt=get_field(reg, C.MAC_CSR0_CHIPSET),
                    rev=get_field(reg, C.MAC_CSR0_REVISION))


def probe_hw_gpio(t: RT5372Transport) -> None:
    """rfkill-switch GPIO direction = input [SRC rt2800lib.c:12053-12059, inside
    rt2800_probe_hw]. The only register op probe_hw emits after the EFUSE dump."""
    reg = t.register_read(C.GPIO_CTRL)
    reg = set_field(reg, C.GPIO_CTRL_DIR2, 1)
    t.register_write(C.GPIO_CTRL, reg)


def write_mac_address(t: RT5372Transport, mac: bytes, u2me_mask: int = 0x00) -> None:
    """Program the chip's self-MAC into MAC_ADDR_DW0/1 (0x1008/0x100c).

    The cold path never sets this, so the MAC-match engine has no identity and the
    autoresponder can't ACK us. ``u2me_mask`` (DW1[23:16]) gates that match: 0 = match
    nothing (monitor default — promiscuous capture); 0xFF = strict-match, so
    active-monitor HW-ACKs only frames to ``mac``.  [SRC rt2800.h MAC_ADDR_DW0/1]"""
    if len(mac) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
    dw0 = mac[0] | (mac[1] << 8) | (mac[2] << 16) | (mac[3] << 24)
    dw1 = (mac[5] << 8) | mac[4] | ((u2me_mask & 0xFF) << 16)
    t.register_write(0x1008, dw0)   # MAC_ADDR_DW0
    t.register_write(0x100C, dw1)   # MAC_ADDR_DW1


def is_chip_warm(t: RT5372Transport) -> bool:
    """True only if wifit3 left this chip inited in monitor mode: safe to skip FW + init."""
    reg = t.register_read(C.PBF_SYS_CTRL)
    if not (reg & C.PBF_SYS_CTRL_READY) or (reg & C.PBF_SYS_CTRL_PRE_INIT):
        return False
    return not (t.register_read(C.RX_FILTER_CFG) & C.RX_FILTER_CFG_DROP_NOT_TO_ME)


# --- radio-on sequence (rt2x00lib_enable_radio + rt2800usb_set_device_state) ---

def set_radio_led(t: RT5372Transport, ev: EepromValues) -> None:
    """Radio LED on — ``rt2800_brightness_set(LED_TYPE_RADIO, enabled)`` [SRC
    rt2800lib.c:1636-1638]. The kernel emits this via the leds-class/rfkill trigger
    as the interface comes up (before STATE_AWAKE on the wire); reproduced here with
    EEPROM-derived args (ledmode from EEPROM_FREQ) so the byte tracks the EEPROM, not
    hardcoded. arg1=0x20 = radio enabled."""
    ledmode = get_field(ev.led_mcu_reg, C.EEPROM_FREQ_LED_MODE)
    t.mcu_request(C.MCU_LED, 0xFF, ledmode, 0x20)


def wakeup(t: RT5372Transport) -> None:
    """STATE_AWAKE [SRC rt2800usb.c:325-334 rt2800usb_set_state]."""
    t.mcu_request(C.MCU_WAKEUP, 0xFF, 0, 2)


def usb_enable_radio_dma(t: RT5372Transport) -> None:
    """USB DMA aggregation setup [SRC rt2800usb.c:296-318 rt2800usb_enable_radio,
    the part before rt2800_enable_radio]."""
    t.wait_wpdma_ready()
    reg = 0
    reg = set_field(reg, C.USB_DMA_CFG_PHY_CLEAR, 0)
    reg = set_field(reg, C.USB_DMA_CFG_RX_BULK_AGG_EN, 0)
    reg = set_field(reg, C.USB_DMA_CFG_RX_BULK_AGG_TIMEOUT, 128)
    # Total RX room in KB, minus 3 to stay under PBF. rx->limit=128.
    reg = set_field(reg, C.USB_DMA_CFG_RX_BULK_AGG_LIMIT,
                    (C.RX_QUEUE_LIMIT * C.DATA_FRAME_SIZE) // 1024 - 3)
    reg = set_field(reg, C.USB_DMA_CFG_RX_BULK_EN, 1)
    reg = set_field(reg, C.USB_DMA_CFG_TX_BULK_EN, 1)
    t.register_write(C.USB_DMA_CFG, reg)


def usb_init_registers(t: RT5372Transport) -> None:
    """Reset the MAC/BBP via USB_MODE_RESET [SRC rt2800usb.c:270-294
    rt2800usb_init_registers] (the chip ``drv_init_registers``)."""
    if not t.wait_csr_ready():
        raise IOError("rt5372: unstable hardware (CSR not ready in usb_init_registers)")
    reg = t.register_read(C.PBF_SYS_CTRL)
    t.register_write(C.PBF_SYS_CTRL, reg & ~0x00002000)
    reg = 0
    reg = set_field(reg, C.MAC_SYS_CTRL_RESET_CSR, 1)
    reg = set_field(reg, C.MAC_SYS_CTRL_RESET_BBP, 1)
    t.register_write(C.MAC_SYS_CTRL, reg)
    t.device_mode_sw(C.USB_MODE_RESET)
    t.register_write(C.MAC_SYS_CTRL, 0x00000000)


def config_filter(t: RT5372Transport, filter_flags: int, monitoring: bool) -> None:
    """RX frame filter [SRC rt2800lib.c:1967-2009 rt2800_config_filter].

    ``filter_flags`` is the mac80211 FIF_* set (``rt2x00mac_configure_filter``
    masks/forces it [SRC rt2x00mac.c:355-401]); ``monitoring`` is the driver's
    CONFIG_MONITORING bit, which alone governs DROP_NOT_TO_ME [[passive_by_default]].
    Init: ``FIF_ALLMULTI`` + ``monitoring=False`` ⇒ 0x1bf97. Interface-up:
    ``ALLMULTI|CONTROL|PSPOLL`` + False ⇒ 0x97. Monitor: same flags + True ⇒ 0x93."""
    reg = t.register_read(C.RX_FILTER_CFG)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_CRC_ERROR, not (filter_flags & C.FIF_FCSFAIL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_PHY_ERROR, not (filter_flags & C.FIF_PLCPFAIL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_NOT_TO_ME, not monitoring)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_NOT_MY_BSSD, 0)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_VER_ERROR, 1)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_MULTICAST, not (filter_flags & C.FIF_ALLMULTI))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_BROADCAST, 0)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_DUPLICATE, 1)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_CF_END_ACK, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_CF_END, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_ACK, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_CTS, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_RTS, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_PSPOLL, not (filter_flags & C.FIF_PSPOLL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_BA, 0)
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_BAR, not (filter_flags & C.FIF_CONTROL))
    reg = set_field(reg, C.RX_FILTER_CFG_DROP_CNTL, not (filter_flags & C.FIF_CONTROL))
    t.register_write(C.RX_FILTER_CFG, reg)


def enable_radio_boot(t: RT5372Transport) -> None:
    """rt2800_enable_radio after init_registers: wait for BBP/RF, fire the boot
    signal, wait for the BBP to come up [SRC rt2800lib.c:10802-10821]."""
    t.wait_bbp_rf_ready()
    t.register_write(C.H2M_BBP_AGENT, 0)
    t.register_write(C.H2M_MAILBOX_CSR, 0)
    t.register_write(C.H2M_INT_SRC, 0)
    t.mcu_request(C.MCU_BOOT_SIGNAL, 0, 0, 0)
    # kernel msleep(1)
    t.wait_bbp_ready()


def enable_radio_finish(t: RT5372Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """rt2800_enable_radio tail [SRC rt2800lib.c:10838-10872]: enable TX, then TX+RX DMA,
    then RX, and push the EEPROM LED config to the MCU. The ``MCU_CURRENT`` current-cal is
    RT3070/3071/3572-only [SRC rt2800lib.c:10829-10836] — NOT RT5392 — so it is not
    emitted. ``chip`` is kept for signature parity with the family (the MCU_CURRENT gate)."""
    reg = t.register_read(C.MAC_SYS_CTRL)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_TX, 1)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_RX, 0)
    t.register_write(C.MAC_SYS_CTRL, reg)
    # kernel udelay(50)
    reg = t.register_read(C.WPDMA_GLO_CFG)
    reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_TX_DMA, 1)
    reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_RX_DMA, 1)
    reg = set_field(reg, C.WPDMA_GLO_CFG_TX_WRITEBACK_DONE, 1)
    t.register_write(C.WPDMA_GLO_CFG, reg)
    reg = t.register_read(C.MAC_SYS_CTRL)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_TX, 1)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_RX, 1)
    t.register_write(C.MAC_SYS_CTRL, reg)

    for cmd, word_idx in ((C.MCU_LED_AG_CONF, C.EEPROM_LED_AG_CONF),
                          (C.MCU_LED_ACT_CONF, C.EEPROM_LED_ACT_CONF),
                          (C.MCU_LED_LED_POLARITY, C.EEPROM_LED_POLARITY)):
        word = ev.word(word_idx)
        t.mcu_request(cmd, 0xFF, word & 0xFF, (word >> 8) & 0xFF)


def start_queue_rx(t: RT5372Transport) -> None:
    """Enable the RX queue [SRC rt2800usb.c:46-67 rt2800usb_start_queue QID_RX].
    Called from rt2x00queue_start_queues after the radio is up."""
    reg = t.register_read(C.MAC_SYS_CTRL)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_RX, 1)
    t.register_write(C.MAC_SYS_CTRL, reg)


def stop_queue_rx(t: RT5372Transport) -> None:
    """Disable the RX queue [SRC rt2800usb.c:69-90 rt2800usb_stop_queue QID_RX].
    Channel/antenna changes require RX off, else the device ignores them
    [SRC rt2x00config.c:143-148]."""
    reg = t.register_read(C.MAC_SYS_CTRL)
    reg = set_field(reg, C.MAC_SYS_CTRL_ENABLE_RX, 0)
    t.register_write(C.MAC_SYS_CTRL, reg)


def _config_wcid_null(t: RT5372Transport, wcid: int) -> None:
    """Write a broadcast (all-0xff) WCID MAC entry [SRC rt2800lib.c:1671-1686
    rt2800_config_wcid with address=NULL]. struct mac_wcid_entry is 8 bytes."""
    t.register_multiwrite(C.mac_wcid_entry(wcid), b"\xff" * 8)


def _delete_wcid_attr(t: RT5372Transport, wcid: int) -> None:
    """[SRC rt2800lib.c:1688-1693 rt2800_delete_wcid_attr]"""
    t.register_write(C.mac_wcid_attr_entry(wcid), 0)


def _clear_beacon_register(t: RT5372Transport, index: int) -> None:
    """Zero a beacon's TXWI to invalidate it [SRC rt2800lib.c:1488-1504
    rt2800_clear_beacon_register]. winfo_size = TXWI_DESC_SIZE_4WORDS (16 B)."""
    base = C.hw_beacon_base(index)
    for off in range(0, C.TXWI_DESC_SIZE_4WORDS, 4):
        t.register_write(base + off, 0)


def init_registers(t: RT5372Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """MAC register block [SRC rt2800lib.c:5836-6375 rt2800_init_registers], RF53xx
    path. Opens with disable_wpdma + drv_init_registers, then the long config block."""
    t.disable_wpdma()
    usb_init_registers(t)

    t.register_write(C.LEGACY_BASIC_RATE, 0x0000013F)
    t.register_write(C.HT_BASIC_RATE, 0x00008003)
    t.register_write(C.MAC_SYS_CTRL, 0x00000000)

    reg = t.register_read(C.BCN_TIME_CFG)
    reg = set_field(reg, C.BCN_TIME_CFG_BEACON_INTERVAL, 1600)
    reg = set_field(reg, C.BCN_TIME_CFG_TSF_TICKING, 0)
    reg = set_field(reg, C.BCN_TIME_CFG_TSF_SYNC, 0)
    reg = set_field(reg, C.BCN_TIME_CFG_TBTT_ENABLE, 0)
    reg = set_field(reg, C.BCN_TIME_CFG_BEACON_GEN, 0)
    reg = set_field(reg, C.BCN_TIME_CFG_TX_TIME_COMPENSATE, 0)
    t.register_write(C.BCN_TIME_CFG, reg)

    config_filter(t, C.FIF_ALLMULTI, monitoring=False)   # rt2800_config_filter(FIF_ALLMULTI)

    reg = t.register_read(C.BKOFF_SLOT_CFG)
    reg = set_field(reg, C.BKOFF_SLOT_CFG_SLOT_TIME, 9)
    reg = set_field(reg, C.BKOFF_SLOT_CFG_CC_DELAY_TIME, 2)
    t.register_write(C.BKOFF_SLOT_CFG, reg)

    # TX_SW_CFG: RT5390/RT5392 arm [SRC rt2800lib.c:5990-5994].
    if chip.is_rt(C.RT5390) or chip.is_rt(C.RT5392):
        t.register_write(C.TX_SW_CFG0, 0x00000404)
        t.register_write(C.TX_SW_CFG1, 0x00080606)
        t.register_write(C.TX_SW_CFG2, 0x00000000)
    else:
        # Generic default [SRC rt2800lib.c:6025-6028]; other chip families out of scope.
        t.register_write(C.TX_SW_CFG0, 0x00000000)
        t.register_write(C.TX_SW_CFG1, 0x00080606)

    reg = t.register_read(C.TX_LINK_CFG)
    reg = set_field(reg, C.TX_LINK_CFG_REMOTE_MFB_LIFETIME, 32)
    reg = set_field(reg, C.TX_LINK_CFG_MFB_ENABLE, 0)
    reg = set_field(reg, C.TX_LINK_CFG_REMOTE_UMFS_ENABLE, 0)
    reg = set_field(reg, C.TX_LINK_CFG_TX_MRQ_EN, 0)
    reg = set_field(reg, C.TX_LINK_CFG_TX_RDG_EN, 0)
    reg = set_field(reg, C.TX_LINK_CFG_TX_CF_ACK_EN, 1)
    reg = set_field(reg, C.TX_LINK_CFG_REMOTE_MFB, 0)
    reg = set_field(reg, C.TX_LINK_CFG_REMOTE_MFS, 0)
    t.register_write(C.TX_LINK_CFG, reg)

    reg = t.register_read(C.TX_TIMEOUT_CFG)
    reg = set_field(reg, C.TX_TIMEOUT_CFG_MPDU_LIFETIME, 9)
    reg = set_field(reg, C.TX_TIMEOUT_CFG_RX_ACK_TIMEOUT, 32)
    reg = set_field(reg, C.TX_TIMEOUT_CFG_TX_OP_TIMEOUT, 10)
    t.register_write(C.TX_TIMEOUT_CFG, reg)

    reg = t.register_read(C.MAX_LEN_CFG)
    reg = set_field(reg, C.MAX_LEN_CFG_MAX_MPDU, C.AGGREGATION_SIZE)
    reg = set_field(reg, C.MAX_LEN_CFG_MAX_PSDU, 3)        # rt2x00_is_usb ⇒ max_psdu=3
    reg = set_field(reg, C.MAX_LEN_CFG_MIN_PSDU, 10)
    reg = set_field(reg, C.MAX_LEN_CFG_MIN_MPDU, 10)
    t.register_write(C.MAX_LEN_CFG, reg)

    reg = t.register_read(C.LED_CFG)
    reg = set_field(reg, C.LED_CFG_ON_PERIOD, 70)
    reg = set_field(reg, C.LED_CFG_OFF_PERIOD, 30)
    reg = set_field(reg, C.LED_CFG_SLOW_BLINK_PERIOD, 3)
    reg = set_field(reg, C.LED_CFG_R_LED_MODE, 3)
    reg = set_field(reg, C.LED_CFG_G_LED_MODE, 3)
    reg = set_field(reg, C.LED_CFG_Y_LED_MODE, 3)
    reg = set_field(reg, C.LED_CFG_LED_POLAR, 1)
    t.register_write(C.LED_CFG, reg)

    t.register_write(C.PBF_MAX_PCNT, 0x1F3FBF9F)

    reg = t.register_read(C.TX_RTY_CFG)
    reg = set_field(reg, C.TX_RTY_CFG_SHORT_RTY_LIMIT, 2)
    reg = set_field(reg, C.TX_RTY_CFG_LONG_RTY_LIMIT, 2)
    reg = set_field(reg, C.TX_RTY_CFG_LONG_RTY_THRE, 2000)
    reg = set_field(reg, C.TX_RTY_CFG_NON_AGG_RTY_MODE, 0)
    reg = set_field(reg, C.TX_RTY_CFG_AGG_RTY_MODE, 0)
    reg = set_field(reg, C.TX_RTY_CFG_TX_AUTO_FB_ENABLE, 1)
    t.register_write(C.TX_RTY_CFG, reg)

    reg = t.register_read(C.AUTO_RSP_CFG)
    reg = set_field(reg, C.AUTO_RSP_CFG_AUTORESPONDER, 1)
    reg = set_field(reg, C.AUTO_RSP_CFG_BAC_ACK_POLICY, 1)
    reg = set_field(reg, C.AUTO_RSP_CFG_CTS_40_MMODE, 1)
    reg = set_field(reg, C.AUTO_RSP_CFG_CTS_40_MREF, 0)
    reg = set_field(reg, C.AUTO_RSP_CFG_AR_PREAMBLE, 0)
    reg = set_field(reg, C.AUTO_RSP_CFG_DUAL_CTS_EN, 0)
    reg = set_field(reg, C.AUTO_RSP_CFG_ACK_CTS_PSM_BIT, 0)
    t.register_write(C.AUTO_RSP_CFG, reg)

    # Protection-config registers (CCK / OFDM / MM20 / MM40 / GF20 / GF40).
    _config_prot(t, C.CCK_PROT_CFG, rate=3, ctrl=0, mm40=0, gf40=0)
    _config_prot(t, C.OFDM_PROT_CFG, rate=3, ctrl=0, mm40=0, gf40=0)
    _config_prot(t, C.MM20_PROT_CFG, rate=0x4004, ctrl=1, cck=0, mm40=0, gf40=0)
    _config_prot(t, C.MM40_PROT_CFG, rate=0x4084, ctrl=1, cck=0, mm40=1, gf40=1)
    _config_prot(t, C.GF20_PROT_CFG, rate=0x4004, ctrl=1, cck=0, mm40=0, gf40=0)
    _config_prot(t, C.GF40_PROT_CFG, rate=0x4084, ctrl=1, cck=0, mm40=1, gf40=1)

    # USB-only: PBF_CFG + re-clear WPDMA_GLO_CFG with burst size [SRC 6172-6186].
    t.register_write(C.PBF_CFG, 0xF40006)
    reg = t.register_read(C.WPDMA_GLO_CFG)
    reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_TX_DMA, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_TX_DMA_BUSY, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_RX_DMA, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_RX_DMA_BUSY, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_WP_DMA_BURST_SIZE, 3)
    reg = set_field(reg, C.WPDMA_GLO_CFG_TX_WRITEBACK_DONE, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_BIG_ENDIAN, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_RX_HDR_SCATTER, 0)
    reg = set_field(reg, C.WPDMA_GLO_CFG_HDR_SEG_LEN, 0)
    t.register_write(C.WPDMA_GLO_CFG, reg)

    reg = t.register_read(C.TXOP_CTRL_CFG)
    reg = set_field(reg, C.TXOP_CTRL_CFG_TIMEOUT_TRUN_EN, 1)
    reg = set_field(reg, C.TXOP_CTRL_CFG_AC_TRUN_EN, 1)
    reg = set_field(reg, C.TXOP_CTRL_CFG_TXRATEGRP_TRUN_EN, 1)
    reg = set_field(reg, C.TXOP_CTRL_CFG_USER_MODE_TRUN_EN, 1)
    reg = set_field(reg, C.TXOP_CTRL_CFG_MIMO_PS_TRUN_EN, 1)
    reg = set_field(reg, C.TXOP_CTRL_CFG_RESERVED_TRUN_EN, 1)   # reserved, but legacy sets it
    reg = set_field(reg, C.TXOP_CTRL_CFG_LSIG_TXOP_EN, 0)
    reg = set_field(reg, C.TXOP_CTRL_CFG_EXT_CCA_EN, 0)
    reg = set_field(reg, C.TXOP_CTRL_CFG_EXT_CCA_DLY, 88)
    reg = set_field(reg, C.TXOP_CTRL_CFG_EXT_CWMIN, 0)
    t.register_write(C.TXOP_CTRL_CFG, reg)

    t.register_write(C.TXOP_HLDR_ET, 0x00000002)

    reg = t.register_read(C.TX_RTS_CFG)
    reg = set_field(reg, C.TX_RTS_CFG_AUTO_RTS_RETRY_LIMIT, 7)
    reg = set_field(reg, C.TX_RTS_CFG_RTS_THRES, C.IEEE80211_MAX_RTS_THRESHOLD)
    reg = set_field(reg, C.TX_RTS_CFG_RTS_FBK_EN, 1)
    t.register_write(C.TX_RTS_CFG, reg)

    t.register_write(C.EXP_ACK_TIME, 0x002400CA)

    # CCK + OFDM SIFS both 16 (Ralink default; CCK 10 breaks 11g+CTS) [SRC 6222-6235].
    reg = t.register_read(C.XIFS_TIME_CFG)
    reg = set_field(reg, C.XIFS_TIME_CFG_CCKM_SIFS_TIME, 16)
    reg = set_field(reg, C.XIFS_TIME_CFG_OFDM_SIFS_TIME, 16)
    reg = set_field(reg, C.XIFS_TIME_CFG_OFDM_XIFS_TIME, 4)
    reg = set_field(reg, C.XIFS_TIME_CFG_EIFS, 314)
    reg = set_field(reg, C.XIFS_TIME_CFG_BB_RXEND_ENABLE, 1)
    t.register_write(C.XIFS_TIME_CFG, reg)

    t.register_write(C.PWR_PIN_CFG, 0x00000003)

    # Clear garbage encryption state: shared-key modes, every WCID + its attr.
    for i in range(4):
        t.register_write(C.shared_key_mode_entry(i), 0)
    for i in range(256):
        _config_wcid_null(t, i)
        _delete_wcid_attr(t, i)
    # Clear IVEIV on a fresh start (kept across a watchdog reset, which we never do).
    for i in range(256):
        t.register_write(C.mac_iveiv_entry(i), 0)
    # Invalidate all 8 hardware beacons.
    for i in range(8):
        _clear_beacon_register(t, i)

    # USB: clock cycle = 30 [SRC 6265-6268].
    reg = t.register_read(C.US_CYC_CNT)
    reg = set_field(reg, C.US_CYC_CNT_CLOCK_CYCLE, 30)
    t.register_write(C.US_CYC_CNT, reg)

    reg = t.register_read(C.HT_FBK_CFG0)
    for mcs, fbk in enumerate((0, 0, 1, 2, 3, 4, 5, 6)):
        reg = set_field(reg, C._NIBBLES[mcs], fbk)
    t.register_write(C.HT_FBK_CFG0, reg)

    reg = t.register_read(C.HT_FBK_CFG1)
    for mcs, fbk in enumerate((8, 8, 9, 10, 11, 12, 13, 14)):
        reg = set_field(reg, C._NIBBLES[mcs], fbk)
    t.register_write(C.HT_FBK_CFG1, reg)

    reg = t.register_read(C.LG_FBK_CFG0)
    for mcs, fbk in enumerate((8, 8, 9, 10, 11, 12, 13, 14)):
        reg = set_field(reg, C._NIBBLES[mcs], fbk)
    t.register_write(C.LG_FBK_CFG0, reg)

    reg = t.register_read(C.LG_FBK_CFG1)
    for mcs, fbk in enumerate((0, 0, 1, 2)):
        reg = set_field(reg, C._NIBBLES[mcs], fbk)
    t.register_write(C.LG_FBK_CFG1, reg)

    # Do not force the BA window size (use the TXWI).
    reg = t.register_read(C.AMPDU_BA_WINSIZE)
    reg = set_field(reg, C.AMPDU_BA_WINSIZE_FORCE_WINSIZE_ENABLE, 0)
    reg = set_field(reg, C.AMPDU_BA_WINSIZE_FORCE_WINSIZE, 0)
    t.register_write(C.AMPDU_BA_WINSIZE, reg)

    # Clear the (cleared-on-read) error counters.
    for reg_addr in (C.RX_STA_CNT0, C.RX_STA_CNT1, C.RX_STA_CNT2,
                     C.TX_STA_CNT0, C.TX_STA_CNT1, C.TX_STA_CNT2):
        t.register_read(reg_addr)

    # Pre-TBTT interrupt leadtime 6ms.
    reg = t.register_read(C.INT_TIMER_CFG)
    reg = set_field(reg, C.INT_TIMER_CFG_PRE_TBTT_TIMER, 6 << 4)
    t.register_write(C.INT_TIMER_CFG, reg)

    # Channel statistics timer.
    reg = t.register_read(C.CH_TIME_CFG)
    reg = set_field(reg, C.CH_TIME_CFG_EIFS_BUSY, 1)
    reg = set_field(reg, C.CH_TIME_CFG_NAV_BUSY, 1)
    reg = set_field(reg, C.CH_TIME_CFG_RX_BUSY, 1)
    reg = set_field(reg, C.CH_TIME_CFG_TX_BUSY, 1)
    reg = set_field(reg, C.CH_TIME_CFG_TMR_EN, 1)
    t.register_write(C.CH_TIME_CFG, reg)


def _config_prot(t: RT5372Transport, reg_addr: int, *, rate: int, ctrl: int,
                 cck: int = 1, mm40: int = 0, gf40: int = 0) -> None:
    """One protection-config register [SRC rt2800lib.c:6094-6170]. The CCK/OFDM regs
    allow CCK TXOP; MM*/GF* don't; the *40 allowances differ per register."""
    reg = t.register_read(reg_addr)
    reg = set_field(reg, C.PROT_CFG_PROTECT_RATE, rate)
    reg = set_field(reg, C.PROT_CFG_PROTECT_CTRL, ctrl)
    reg = set_field(reg, C.PROT_CFG_PROTECT_NAV_SHORT, 1)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_CCK, cck)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_OFDM, 1)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_MM20, 1)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_MM40, mm40)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_GF20, 1)
    reg = set_field(reg, C.PROT_CFG_TX_OP_ALLOW_GF40, gf40)
    reg = set_field(reg, C.PROT_CFG_RTS_TH_EN, 0)
    t.register_write(reg_addr, reg)
