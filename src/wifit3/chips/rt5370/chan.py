"""Channel tuning + TX-power for the RT5370 (RT5390, RF5370, 1T1R, 2.4 GHz).

Ported from ``rt2800lib.c``: ``rt2800_config_channel`` dispatch (4161) →
``rt2800_config_channel_rf53xx`` (3387) for RF5370; the per-tune VCO calibration
(4228-4255); ``rt2800_config_txpower`` → ``rt2800_config_txpower_rt28xx`` (5338) + the
EEPROM TX-power decode; plus ``rt2800_config_lna_gain`` (2401), ``rt2800_update_survey``
(1255), ``rt2800_config_ant`` (2322), ``rt2800_freq_cal_mode1`` (2447). Confirmed against
captures_rt2800usb_rt5370/capture-1 by ``verify_pcap.py rt5370``.

``set_channel`` is the whole ``rt2x00mac_config(CHANGE_CHANNEL)`` wrapper: stop the RX
queue, run ``rt2800_config`` (lna-gain → survey → channel → txpower), then the antenna
reconfigure (config_ant + reset_tuner), and let the antenna's refcounted ``start_queue``
re-enable RX [SRC rt2x00mac.c:307-352, rt2x00config.c:104-163].

Scope: RF53xx 2.4 GHz / 1T1R. RT5390 has NO RX-filter loopback calibration, so the tune
takes no init-derived calibration state (``drv`` is unused / ``None``). 5 GHz and the
foreign-family / 40 MHz arms are out of scope (this driver claims only 148f:5370).
"""
from __future__ import annotations

from . import constants as C
from . import mac
from .constants import ChipInfo, get_field, set_field
from .eeprom import EepromValues
from .link_tuner import reset_tuner
from .transport import RT5370Transport


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


# ---------------------------------------------------------------------------
# TX-power EEPROM decode
# ---------------------------------------------------------------------------

def txpower_to_dev(channel: int, txpower: int) -> int:
    """Clamp a per-channel EEPROM TX-power byte to the chip's range [SRC
    rt2800lib.c:4112-4129 rt2800_txpower_to_dev]."""
    if channel <= 14:
        return _clamp(txpower, C.MIN_G_TXPOWER, C.MAX_G_TXPOWER)
    # #TODO untestable: 5 GHz — this card is 2.4 GHz only.
    return _clamp(txpower, C.MIN_A_TXPOWER, C.MAX_A_TXPOWER)


def _get_txpower_bw_comp(ev: EepromValues) -> int:
    """HT40 TX-power compensation [SRC rt2800lib.c:4683-4725]. We only ever tune 20 MHz
    channels, so CONFIG_CHANNEL_HT40 is never set ⇒ always 0."""
    return 0


def _compensate_txpower(ev: EepromValues, is_rate_b: int, power_level: int,
                        txpower: int, delta: int) -> int:
    """Per-rate TX-power compensation [SRC rt2800lib.c:4748-4797]. With
    CAPABILITY_POWER_LIMIT unset the EIRP criterion collapses to reg_limit=0."""
    if ev.power_limit:
        # #TODO untestable: needs the EEPROM EIRP criterion + runtime power_level.
        criterion = get_field(ev.word(C.EEPROM_TXPOWER_BYRATE + 1),
                              C._EEPROM_TXPOWER_BYRATE_RATE[0])
        eirp_crit = get_field(ev.word(C.EEPROM_EIRP_MAX_TX_POWER),
                              C.EEPROM_EIRP_MAX_TX_POWER_2GHZ)
        eirp = eirp_crit + (txpower - criterion) + (4 if is_rate_b else 0) + delta
        reg_limit = (eirp - power_level) if eirp > power_level else 0
    else:
        reg_limit = 0
    return min(max(0, txpower + delta - reg_limit), 0xC)


# ---------------------------------------------------------------------------
# Per-tune helpers
# ---------------------------------------------------------------------------

def config_lna_gain(ev: EepromValues, channel: int) -> int:
    """Recompute the LNA gain from the EEPROM before each config [SRC
    rt2800lib.c:2401-2438 rt2800_config_lna_gain]. EEPROM-cache read, no wire op."""
    if channel <= 14:
        return ev.lna_gain_bg
    return get_field(ev.word(C.EEPROM_LNA), 0xFF00)   # #TODO untestable: 5 GHz LNA_A0


def update_survey(t: RT5370Transport) -> None:
    """Snapshot channel idle/busy counters before a switch [SRC rt2800lib.c:1255-1264
    rt2800_update_survey]. Cleared-on-read, so issuing the reads is the point."""
    t.register_read(C.CH_IDLE_STA)
    t.register_read(C.CH_BUSY_STA)
    t.register_read(C.CH_BUSY_STA_SEC)


def config_ant(t: RT5370Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """RX/TX antenna select via BBP1/BBP3 [SRC rt2800lib.c:2322-2398 rt2800_config_ant].
    This card is 1T1R (txpath=rxpath=1) → BBP1 TX_ANTENNA=0, BBP3 RX_ANTENNA=0. The 2T2R
    and 3T3R arms are #TODO untestable here."""
    r1 = t.bbp_read(1)
    r3 = t.bbp_read(3)

    if ev.tx_chain_num == 1:
        r1 = set_field(r1, C.BBP1_TX_ANTENNA, 0)
    elif ev.tx_chain_num == 2:
        r1 = set_field(r1, C.BBP1_TX_ANTENNA, 2)
    else:
        r1 = set_field(r1, C.BBP1_TX_ANTENNA, 2)   # #TODO untestable: 3T3R

    if ev.rx_chain_num == 1:
        # The SW-diversity sub-branch is RT3070/3090/3352/3390-only — never on RT5390.
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 0)
    elif ev.rx_chain_num == 2:
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 1)
    else:
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 2)   # #TODO untestable: 3T3R

    t.bbp_write(3, r3)
    t.bbp_write(1, r1)


# ---------------------------------------------------------------------------
# Channel tune
# ---------------------------------------------------------------------------

def freq_cal_mode1(t: RT5370Transport, ev: EepromValues) -> None:
    """Apply the crystal frequency trim [SRC rt2800lib.c:2447-2480 rt2800_freq_cal_mode1].
    On USB the host issues one MCU_FREQ_OFFSET request and the MCU walks RFCSR17
    internally; the host-side incremental RFCSR17 loop is the non-USB (PCI) path."""
    freq_offset = min(get_field(ev.freq_offset, C.RFCSR17_CODE), C.FREQ_OFFSET_BOUND)
    rfcsr = t.rfcsr_read(17)
    prev_rfcsr = rfcsr
    rfcsr = set_field(rfcsr, C.RFCSR17_CODE, freq_offset)
    if rfcsr == prev_rfcsr:
        return
    # USB: the MCU does the incremental walk (the host RFCSR17 loop is #TODO untestable PCI).
    t.mcu_request(C.MCU_FREQ_OFFSET, 0xFF, freq_offset, prev_rfcsr)


def config_channel_rf53xx(t: RT5370Transport, chip: ChipInfo, ev: EepromValues,
                          rf: tuple[int, int, int], default_power1: int,
                          default_power2: int, channel: int) -> None:
    """RF53xx channel program [SRC rt2800lib.c:3387-3483 rt2800_config_channel_rf53xx].
    ``rf`` is (rf1, rf2, rf3): rf1→RFCSR8, rf3→RFCSR9, rf2→RFCSR11.R. TX power →
    RFCSR49 (RFCSR50 is RT5392-only); freq trim via the MCU; per-channel RFCSR55+59
    from the rev-F non-BT tables (RT5390F) — see the tail."""
    t.rfcsr_write(C.RFCSR8, rf[0])
    t.rfcsr_write(C.RFCSR9, rf[2])
    rfcsr = t.rfcsr_read(11)
    rfcsr = set_field(rfcsr, C.RFCSR11_R, rf[1])
    t.rfcsr_write(11, rfcsr)

    rfcsr = t.rfcsr_read(49)
    rfcsr = set_field(rfcsr, C.RFCSR49_TX, min(default_power1, C.POWER_BOUND))
    t.rfcsr_write(49, rfcsr)

    if chip.is_rt(C.RT5392):
        rfcsr = t.rfcsr_read(50)
        rfcsr = set_field(rfcsr, C.RFCSR50_TX, min(default_power2, C.POWER_BOUND))
        t.rfcsr_write(50, rfcsr)

    rfcsr = t.rfcsr_read(1)
    if chip.is_rt(C.RT5392):
        rfcsr = set_field(rfcsr, C.RFCSR1_RX1_PD, 1)
        rfcsr = set_field(rfcsr, C.RFCSR1_TX1_PD, 1)
    rfcsr = set_field(rfcsr, C.RFCSR1_RF_BLOCK_EN, 1)
    rfcsr = set_field(rfcsr, C.RFCSR1_PLL_PD, 1)
    rfcsr = set_field(rfcsr, C.RFCSR1_RX0_PD, 1)
    rfcsr = set_field(rfcsr, C.RFCSR1_TX0_PD, 1)
    t.rfcsr_write(1, rfcsr)

    freq_cal_mode1(t, ev)

    # Per-channel r55/r59 tables [SRC rt2800lib.c:3431-3482], gated on the EEPROM
    # NIC_CONF1 BT_COEXIST (runtime, not silicon) so a BT-combo 148f:5370 card comes up
    # instead of failing loud. The reference card is bt_coexist-clear + rev-F → the
    # non-BT _rev arm, byte-identical.
    if ev.bt_coexist:
        if chip.rt_rev_gte(C.RT5390, C.REV_RT5390F):
            t.rfcsr_write(55, C.RF55_BT_REV[channel - 1])
            t.rfcsr_write(59, C.RF59_BT_REV[channel - 1])
        else:
            t.rfcsr_write(59, C.RF59_BT[channel - 1])
    elif chip.rt_rev_gte(C.RT5390, C.REV_RT5390F):
        # rev >= REV_RT5390F (this card, rev 0x0502): RFCSR55 + RFCSR59 from the _rev
        # tables [SRC rt2800lib.c:3453-3464].
        t.rfcsr_write(55, C.RF55_NON_BT_REV[channel - 1])
        t.rfcsr_write(59, C.RF59_NON_BT_REV[channel - 1])
    else:
        # RT5390 pre-F / RT5392 / RT6352: only RFCSR59 from the shared non-BT table
        # [SRC rt2800lib.c:3465-3473]. #TODO untestable here (this unit is rev-F).
        t.rfcsr_write(59, C.RF59_NON_BT[channel - 1])


def config_channel(t: RT5370Transport, chip: ChipInfo, ev: EepromValues,
                   channel: int, lna_gain: int) -> None:
    """Program the radio + baseband for ``channel`` [SRC rt2800lib.c:4161-4564
    rt2800_config_channel], RF53xx / 2.4 GHz / 1T1R path."""
    power1 = txpower_to_dev(channel, ev.power_byte(C.EEPROM_TXPOWER_BG1, channel - 1))
    power2 = txpower_to_dev(channel, ev.power_byte(C.EEPROM_TXPOWER_BG2, channel - 1))

    rf = C.RF_VALS_3X_2G[channel]
    config_channel_rf53xx(t, chip, ev, rf, power1, power2, channel)

    # Per-tune VCO calibration (RF53xx) [SRC 4228-4255]: H20M=0 at 20 MHz, then VCOCAL_EN.
    rfcsr = t.rfcsr_read(30)
    rfcsr = set_field(rfcsr, C.RFCSR30_TX_H20M, 0)
    rfcsr = set_field(rfcsr, C.RFCSR30_RX_H20M, 0)
    t.rfcsr_write(30, rfcsr)
    rfcsr = t.rfcsr_read(3)
    rfcsr = set_field(rfcsr, C.RFCSR3_VCOCAL_EN, 1)
    t.rfcsr_write(3, rfcsr)

    # BBP gain-track + BBP86 [SRC 4298-4306]: generic arm; RT5392 → BBP86=0.
    t.bbp_write(62, 0x37 - lna_gain)
    t.bbp_write(63, 0x37 - lna_gain)
    t.bbp_write(64, 0x37 - lna_gain)
    t.bbp_write(86, 0x00)

    # The channel<=14 BBP82/75 LNA block [SRC 4308-4326] is excluded for RT5390/5392/6352.

    reg = t.register_read(C.TX_BAND_CFG)
    reg = set_field(reg, C.TX_BAND_CFG_HT40_MINUS, 0)
    reg = set_field(reg, C.TX_BAND_CFG_A, int(channel > 14))
    reg = set_field(reg, C.TX_BAND_CFG_BG, int(channel <= 14))
    t.register_write(C.TX_BAND_CFG, reg)

    # TX_PIN_CFG: PA/LNA path enables [SRC 4356-4411]. tx_pin starts at 0 (non-RT6352).
    tx_pin = 0
    tx_pin = _config_tx_pin_pa(tx_pin, ev.tx_chain_num, channel, ev.bt_coexist)
    tx_pin = _config_tx_pin_lna(tx_pin, ev.rx_chain_num)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_RFTR_EN, 1)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_TRSW_EN, 1)
    t.register_write(C.TX_PIN_CFG, tx_pin)

    bbp = t.bbp_read(4)
    bbp = set_field(bbp, C.BBP4_BANDWIDTH, 0)        # 2 * conf_is_ht40 = 0
    t.bbp_write(4, bbp)

    bbp = t.bbp_read(3)
    bbp = set_field(bbp, C.BBP3_HT40_MINUS, 0)
    t.bbp_write(3, bbp)
    # kernel usleep_range(1000, 1500)

    # Clear channel-statistic counters [SRC 4548-4553].
    t.register_read(C.CH_IDLE_STA)
    t.register_read(C.CH_BUSY_STA)
    t.register_read(C.CH_BUSY_STA_SEC)


def _config_tx_pin_pa(tx_pin: int, tx_chain_num: int, channel: int,
                      bt_coexist: bool) -> int:
    """TX_PIN_CFG PA-enable switch [SRC rt2800lib.c:4363-4388]. This card is 1T1R so only
    the primary G0 PA is enabled; the secondary (2T2R) and tertiary (3T3R) arms are #TODO
    untestable here. A BT-combo card forces G0 on unconditionally [SRC :4382-4386] — on
    2.4 GHz (is_g=1) that equals the non-BT value, so it never changes the reference wire."""
    is_a = int(channel > 14)
    is_g = int(channel <= 14)
    if tx_chain_num >= 3:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A2_EN, is_a)   # #TODO untestable: 3T3R
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G2_EN, is_g)
    if tx_chain_num >= 2:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A1_EN, is_a)
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G1_EN, is_g)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A0_EN, is_a)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G0_EN, 1 if bt_coexist else is_g)
    return tx_pin


def _config_tx_pin_lna(tx_pin: int, rx_chain_num: int) -> int:
    """TX_PIN_CFG LNA-enable switch [SRC rt2800lib.c:4390-4406]."""
    if rx_chain_num >= 3:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A2_EN, 1)     # #TODO untestable: 3T3R
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G2_EN, 1)
    if rx_chain_num >= 2:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A1_EN, 1)
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G1_EN, 1)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A0_EN, 1)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G0_EN, 1)
    return tx_pin


def config_txpower(t: RT5370Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """Program the per-rate TX power [SRC rt2800lib.c:5338-5519
    rt2800_config_txpower_rt28xx], 2.4 GHz. The TSSI gain-calibration delta is
    RT3070/3071/3090/3572-only — N/A for RT5390, so delta stays 0."""
    delta = _get_txpower_bw_comp(ev)                      # 0 (20 MHz only)
    # reg_delta = min(power_level - max_power, 0) = 0 (monitor tunes to the regulatory max).

    if delta <= -12:
        power_ctrl, delta = 2, delta + 12
    elif delta <= -6:
        power_ctrl, delta = 1, delta + 6
    else:
        power_ctrl = 0

    r1 = t.bbp_read(1)
    r1 = set_field(r1, C.BBP1_TX_POWER_CTRL, power_ctrl)
    t.bbp_write(1, r1)

    offset = C.TX_PWR_CFG_0
    for i in range(0, C.EEPROM_TXPOWER_BYRATE_SIZE, 2):
        if offset > C.TX_PWR_CFG_4:
            break
        reg = t.register_read(offset)
        lo = ev.word(C.EEPROM_TXPOWER_BYRATE + i)
        hi = ev.word(C.EEPROM_TXPOWER_BYRATE + i + 1)
        for k in range(4):
            tp = get_field(lo, C._EEPROM_TXPOWER_BYRATE_RATE[k])
            tp = _compensate_txpower(ev, 1 if i == 0 else 0, 0, tp, delta)
            reg = set_field(reg, C._TX_PWR_CFG_RATE[k], tp)
        for k in range(4):
            tp = get_field(hi, C._EEPROM_TXPOWER_BYRATE_RATE[k])
            tp = _compensate_txpower(ev, 0, 0, tp, delta)
            reg = set_field(reg, C._TX_PWR_CFG_RATE[4 + k], tp)
        t.register_write(offset, reg)
        offset += 4


# ---------------------------------------------------------------------------
# set_channel — the full rt2x00mac_config(CHANGE_CHANNEL) wrapper
# ---------------------------------------------------------------------------

def set_channel(t: RT5370Transport, chip: ChipInfo, ev: EepromValues,
                drv, channel: int) -> None:
    """Tune to ``channel`` exactly as ``rt2x00mac_config(CHANGE_CHANNEL)`` does
    [SRC rt2x00mac.c:307-352, rt2x00config.c:193-280 + 104-163]:
        stop RX → [lna_gain → survey → config_channel → config_txpower] →
        reset_tuner → config_ant → reset_tuner → start RX

    ``drv`` is unused (RT5390 threads no init-derived calibration); kept for the family
    set_channel signature."""
    mac.stop_queue_rx(t)
    lna_gain = config_lna_gain(ev, channel)
    update_survey(t)
    config_channel(t, chip, ev, channel, lna_gain)
    config_txpower(t, chip, ev)
    reset_tuner(t, chip, lna_gain)        # rt2x00lib_config (CHANGE_CHANNEL) tail
    config_ant(t, chip, ev)               # rt2x00lib_config_antenna
    reset_tuner(t, chip, lna_gain)        # config_antenna's reset_tuner(antenna=true)
    mac.start_queue_rx(t)                 # config_antenna's refcounted start_queue
