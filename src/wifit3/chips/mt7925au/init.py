"""MT7925AU post-boot device init + monitor entry.

Ported from mt7925_register_device / mt7925_mac_init / the mac80211 start path
(mt7925/init.c, mt7925/mac.c, mt792x_mac.c, mt7925/main.c). Runs after firmware is
up, in wire order: the run_firmware tail (GET_NIC_CAPAB, fw_log), FW_DL_EN clear,
set_eeprom, the mac_init register block, then monitor entry.
"""
import logging
from dataclasses import dataclass

from . import mac, mcu, txpower
from .transport import MT7925AUTransport
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


@dataclass
class InitState:
    """What the post-boot init learned from the firmware (GET_NIC_CAPAB reply)."""
    caps: mcu.NicCaps


def _field_prep(mask: int, val: int) -> int:
    """FIELD_PREP(mask, val): shift val into the mask's low bit."""
    shift = (mask & -mask).bit_length() - 1
    return (val << shift) & mask


async def post_boot_init(transport: MT7925AUTransport) -> InitState:
    """mt7925 post-boot init in wire order. Returns the per-card caps read from firmware."""
    # Phase A — mt7925_run_firmware tail (mt7925/mcu.c:1053): GET_NIC_CAPAB (reply
    # carries the card MAC), then fw_log_2_host. load_clc emits no MCU here.
    resp = await transport.send_mcu_command(*mcu.get_nic_capability())
    caps = mcu.parse_nic_capability(resp or b"")
    logger.debug("MT7925AU NIC caps: MAC=%s antenna_mask=0x%x bands 2.4=%d 5=%d 6=%d",
                caps.mac, caps.antenna_mask, caps.has_2ghz, caps.has_5ghz, caps.has_6ghz)
    await transport.send_mcu_command(*mcu.fw_log_2_host(1))

    # Phase B — mt7925u_mcu_init tail (mt7925/usb.c:70): clear FW_DL_EN.
    transport.clear_bits(MT_UDMA_TX_QSEL, MT_FW_DL_EN)

    # Phase C — __mt7925_init_hardware (mt7925/init.c): set_eeprom.
    await transport.send_mcu_command(*mcu.set_eeprom())

    # Phase D — mt7925_mac_init register block.
    _mac_init(transport)

    # Phase E — mt7925_init_work tail (mt7925/init.c:164-192): mt76_register_device's
    # world-"00" reg notifier (regd.c:200 -> mt7925_mcu_regd_update: no CLC on USB, then
    # channel domain + rate TX power), thermal protection, deep sleep.
    await transport.send_mcu_command(*mcu.set_channel_domain())
    for cmd, payload in txpower.rate_txpower_all(caps.has_6ghz):
        await transport.send_mcu_command(cmd, payload)
    await transport.send_mcu_command(*mcu.thermal_gband(), wait_resp=False)
    await transport.send_mcu_command(*mcu.thermal_aband(), wait_resp=False)
    await transport.send_mcu_command(*mcu.set_deep_sleep(False), wait_resp=False)

    return InitState(caps=caps)


async def start(t: MT7925AUTransport) -> None:
    """__mt7925_start (mt7925/main.c:316) up to the mac_work queue, which the driver owns."""
    await t.send_mcu_command(*mcu.set_channel_domain())
    await t.send_mcu_command(*mcu.set_rts_thresh())
    mac.reset_counters(t)


async def enter_monitor(transport: MT7925AUTransport, channel: int) -> None:
    """Bring the chip into monitor mode on ``channel`` (the mac80211 add_interface +
    config(MONITOR) + configure_filter path)."""
    t = transport
    _wtbl_update(t, MT792x_WTBL_RESERVED)
    await t.send_mcu_command(*mcu.uni_dev_info(True))
    await t.send_mcu_command(*mcu.uni_bss_info(True))
    # change_chanctx for the monitor vif (main.c:1780-1782).
    await t.send_mcu_command(*mcu.set_sniffer(True))
    await t.send_mcu_command(*mcu.config_sniffer(channel))
    monitor_filter = mcu.MT_FILTER_ENABLE | mcu.MT_FILTER_CONTROL | mcu.MT_FILTER_OTHER_BSS
    # configure_filter (main.c:808).
    await t.send_mcu_command(*mcu.set_rxfilter(monitor_filter))
    # config(CHANGE_MONITOR) -> mt7925_sniffer_interface_iter (main.c:737-744), whose
    # set_beacon_filter(false) is set_bss_pm(false) + the DROP_OTHER_BEACON clear (mcu.c:2060-2066).
    await t.send_mcu_command(*mcu.set_sniffer(True))
    await t.send_mcu_command(*mcu.set_deep_sleep(False), wait_resp=False)
    await t.send_mcu_command(*mcu.uni_bss_pm_disable())
    await t.send_mcu_command(*mcu.set_rxfilter(0, mcu.MT_WF_RFCR_DROP_OTHER_BEACON,
                                              mcu.MT7925_FIF_BIT_CLR))
    # configure_filter again, from mac80211's interface-up filter reconfig.
    await t.send_mcu_command(*mcu.set_rxfilter(monitor_filter))


def _mac_init(t: MT7925AUTransport) -> None:
    """mt7925_mac_init (mt7925/init.c:78): MDP de-agg, per-WCID admission clear,
    per-band MAC/MIB/DMA setup, and the 12 basic-rate fixed-rate table entries."""
    # MDP: max RX len + de-aggregation (init.c:82-84).
    t.rmw(MT_MDP_DCR1, MT_MDP_DCR1_MAX_RX_LEN, _field_prep(MT_MDP_DCR1_MAX_RX_LEN, MDP_MAX_RX_LEN))
    t.set_bits(MT_MDP_DCR0, MT_MDP_DCR0_DAMSDU_EN)

    # Per-WCID admission-count clear, idx 0..MT792x_WTBL_SIZE-1 (init.c:86).
    for i in range(MT792x_WTBL_SIZE):
        _wtbl_update(t, i)

    # Per-band MAC/MIB/DMA setup (mt792x_mac_init_band), band 0 then 1.
    for band in range(2):
        _mac_init_band(t, band)

    # Basic-rate fixed-rate table (mt7925_mac_init_basic_rates).
    for i, rate_idx in enumerate(BASIC_RATE_IDX):
        _set_fixed_rate(t, MT792x_BASIC_RATES_TBL + i, rate_idx)


def _wtbl_update(t: MT7925AUTransport, idx: int) -> None:
    """mt7925_mac_wtbl_update (mt7925/mac.c:13): program the WCID + ADM_COUNT_CLEAR,
    then poll BUSY clear."""
    t.rmw(MT_WTBL_UPDATE, MT_WTBL_UPDATE_WLAN_IDX,
          (idx & MT_WTBL_UPDATE_WLAN_IDX) | MT_WTBL_UPDATE_ADM_COUNT_CLEAR)
    for _ in range(50):
        if not (t.read_reg32(MT_WTBL_UPDATE) & MT_WTBL_UPDATE_BUSY):
            break


def _mac_init_band(t: MT7925AUTransport, b: int) -> None:
    """mt792x_mac_init_band (mt792x_mac.c:285): nine RMW ops per band."""
    t.rmw(MT_TMAC_CTCR0(b), MT_TMAC_CTCR0_INS_DDLMT_REFTIME,
          _field_prep(MT_TMAC_CTCR0_INS_DDLMT_REFTIME, TMAC_CTCR0_REFTIME_VAL))
    t.set_bits(MT_TMAC_CTCR0(b),
               MT_TMAC_CTCR0_INS_DDLMT_VHT_SMPDU_EN | MT_TMAC_CTCR0_INS_DDLMT_EN)
    t.set_bits(MT_WF_RMAC_MIB_TIME0(b), MT_WF_RMAC_MIB_RXTIME_EN)
    t.set_bits(MT_WF_RMAC_MIB_AIRTIME0(b), MT_WF_RMAC_MIB_RXTIME_EN)
    t.set_bits(MT_MIB_SCR1(b), MT_MIB_TXDUR_EN)
    t.set_bits(MT_MIB_SCR1(b), MT_MIB_RXDUR_EN)
    t.rmw(MT_DMA_DCR0(b), MT_DMA_DCR0_MAX_RX_LEN,
          _field_prep(MT_DMA_DCR0_MAX_RX_LEN, DMA_DCR0_MAX_RX_LEN_VAL))
    t.clear_bits(MT_DMA_DCR0(b), MT_DMA_DCR0_RXD_G5_EN)
    t.rmw(MT_WTBLOFF_TOP_RSCR(b),
          MT_WTBLOFF_TOP_RSCR_RCPI_MODE | MT_WTBLOFF_TOP_RSCR_RCPI_PARAM,
          _field_prep(MT_WTBLOFF_TOP_RSCR_RCPI_PARAM, RSCR_RCPI_PARAM_VAL))


def _set_fixed_rate(t: MT7925AUTransport, tbl_idx: int, rate_idx: int) -> None:
    """mt7925_mac_set_fixed_rate_table (mt7925/mac.c:157): three WTBL indirect writes."""
    t.write_reg32(MT_WTBL_ITDR0, rate_idx)
    t.write_reg32(MT_WTBL_ITDR1, MT_WTBL_SPE_IDX_SEL)
    t.write_reg32(MT_WTBL_ITCR, MT_WTBL_ITCR_WR | MT_WTBL_ITCR_EXEC | tbl_idx)
