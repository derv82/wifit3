"""RTL8188FTV DKMS beacon/burst/agg/turn-on tail (M5e).

Ported from ``rtl8188f_InitBeaconParameters`` (hal/rtl8188f/rtl8188f_hal_init.c:
2870-2902; ``InitBeaconMaxError`` is a no-op, CONFIG_ADHOC_WORKAROUND_SETTING
off), ``_InitBurstPktLen`` + ``usb_AggSettingTxUpdate`` /
``usb_AggSettingRxUpdate`` (hal/rtl8188f/usb/usb_halinit.c), ``_InitHWLed``
(no-op: ``_ReadLEDSetting`` is commented out so LedStrategy != HW_LED), and
``_BBTurnOnBlock`` (OFDM/CCK enable). BT_COEXIST off; AMPDUBurstMode unset.
"""
from __future__ import annotations

import time

from . import bb


def BIT(n: int) -> int:
    return 1 << n


def init_beacon_params(t, station: bool = True) -> None:
    # mlme fw_state inits to WIFI_STATION_STATE (rtw_mlme.c:44), so the
    # DRVERLYINT write is skipped on a fresh bring-up.
    val16 = 0x10 | (0x10 << 8)
    t.write16(0x0550, val16)
    t.write16(0x0540, 0x6404)
    if not station:
        t.write8(0x0558, 0x05)
    t.write8(0x0559, 0x02)
    t.write16(0x0510, 0x660F)
    t.read8(0x0550)
    t.read8(0x0522)
    t.read8(0x0422)
    t.read8(0x0542)
    t.read8(0x0101)


def init_burst(t, bulk_out_size: int = 512, ampdu_burst_mode: bool = False) -> None:
    tmp8 = t.read8(0x0290)
    tmp8 &= ~(BIT(4) | BIT(5))
    if bulk_out_size == 512:
        tmp8 |= BIT(4)
    else:
        tmp8 |= BIT(5)
    tmp8 |= BIT(1) | BIT(2) | BIT(3)
    t.write8(0x0290, tmp8)
    tmp8 = t.read8(0x04C7)
    t.write8(0x04C7, tmp8 | BIT(7))
    t.write16(0x04CA, 0x0C14)
    t.write8(0x0456, 0x70)
    t.write32(0x0458, 0xFFFFFFFF)
    if ampdu_burst_mode:
        t.write8(0x04BC, 0x5F)
    t.write8(0x060C, 0x18)
    t.write8(0x0512, 0x00)
    t.write8(0x0420, 0x80)
    t.write32(0x0460, 0x03086666)
    t.write8(0x055C, 0x28)
    t.write8(0x0638, 0x28)
    tmp8 = t.read8(0x001C)
    t.write8(0x001C, tmp8 | BIT(5) | BIT(6))


def agg_tx_update(t, tx_agg_desc_num: int = 0x6, wifi_spec: bool = False) -> None:
    if wifi_spec:
        return
    value32 = t.read32(0x0208)
    value32 = (value32 & ~(0xF << 4)) | ((tx_agg_desc_num & 0xF) << 4)
    t.write32(0x0208, value32)
    t.write8(0x0228, (tx_agg_desc_num << 1) & 0xFF)


def agg_rx_update(t, block_count: int = 0x5, block_timeout: int = 0x20) -> None:
    # USB_RX_AGG_USB arm (interface_configure mode on this build); DMA and
    # DISABLE arms are TODO, untested here.
    aggctrl = t.read8(0x010C)
    aggctrl &= ~BIT(2)
    aggrx = t.read32(0x0280)
    aggrx &= ~(1 << 31)
    aggrx &= ~0xFF0F
    rxdmamode = t.read8(0x0290)
    aggctrl |= BIT(2)
    aggrx &= ~(1 << 31)
    aggrx |= block_count & 0xF
    aggrx |= (block_timeout << 8)
    rxdmamode |= BIT(1)
    t.write8(0x010C, aggctrl)
    t.write32(0x0280, aggrx)
    t.write8(0x0290, rxdmamode)
    time.sleep(1e-6)


def init_hw_led(t, led_strategy_hw: bool = False) -> None:
    if not led_strategy_hw:
        return


def drop_incorrect_bulk_out(t) -> None:
    value32 = t.read32(0x020C)
    t.write32(0x020C, value32 | BIT(9))


def mcast2uni_lifetime(t) -> None:
    t.write16(0x04C0, 0x0400)
    t.write16(0x04C2, 0x0400)


def turn_on_block(t) -> None:
    bb.set_bb_reg(t, 0x800, 0x1000000, 0x1)
    bb.set_bb_reg(t, 0x800, 0x2000000, 0x1)


def misc11_tail(t) -> None:
    t.write8(0x0423, 0xFF)
    t.write32(0x04CC, 0x0201FFFF)


def init_gpio_setting(t) -> None:
    value8 = t.read8(0x0040)
    t.write8(0x0040, value8 & ~BIT(5))
