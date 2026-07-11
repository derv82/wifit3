"""Channel tuning + TX-power for the RT3070 (RF3020, 1T1R, 2.4 GHz).

Ported from ``rt2800lib.c``: ``rt2800_config_channel`` dispatch (4161) →
``rt2800_config_channel_rf3xxx`` (2547) for RF3020; ``rt2800_config_txpower`` (5521)
→ ``rt2800_config_txpower_rt28xx`` (5338) + the EEPROM TX-power decode
(``rt2800_txpower_to_dev`` 4112, ``rt2800_compensate_txpower`` 4748); plus the per-tune
helpers ``rt2800_config_lna_gain`` (2401), ``rt2800_update_survey`` (1255),
``rt2800_config_ant`` (2322). Confirmed against capture-1 by ``verify_pcap.py rt3070``.

``set_channel`` is the whole ``rt2x00mac_config(CHANGE_CHANNEL)`` wrapper: it stops the
RX queue, runs ``rt2800_config`` (lna-gain → survey → channel → txpower), then the
antenna reconfigure (``rt2x00lib_config_antenna``: config_ant + reset_tuner), and lets
the antenna's refcounted ``start_queue`` re-enable RX [SRC rt2x00mac.c:307-352,
rt2x00config.c:104-163]. That is the 120-op block airodump/iw repeats per hop.

Scope: RF3020 (and its RF2020/3021/3022/3320 2.4 GHz siblings) take ``config_channel_rf3xxx``;
the other RF families and the RT3352/3593/3883/5592/6352 BBP arms are different silicon
this driver does not claim, so — like ``mac.init_registers`` — only the reachable RF30xx
2.4 GHz path is transcribed. ``config_channel`` runs any RF companion (incl. an unburned /
mislabeled EEPROM RF_TYPE) on that RF30xx silicon-default tune rather than -ENODEV; the
driver names + flags an untested variant once at connect [SRC rt2800lib.c:4185-4227].
"""
from __future__ import annotations

from . import constants as C
from . import mac
from .constants import ChipInfo, get_field, set_field
from .eeprom import EepromValues
from .link_tuner import reset_tuner
from .state import DrvData
from .transport import RT3070Transport


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


# ---------------------------------------------------------------------------
# TX-power EEPROM decode
# ---------------------------------------------------------------------------

def txpower_to_dev(channel: int, txpower: int) -> int:
    """Clamp a per-channel EEPROM TX-power byte to the chip's range [SRC
    rt2800lib.c:4112-4129 rt2800_txpower_to_dev]. The RT3593/RT3883 ALC field is
    different silicon (out of scope)."""
    if channel <= 14:
        return _clamp(txpower, C.MIN_G_TXPOWER, C.MAX_G_TXPOWER)
    # #TODO untestable: 5 GHz — this card is 2.4 GHz only.
    return _clamp(txpower, C.MIN_A_TXPOWER, C.MAX_A_TXPOWER)


def _get_txpower_bw_comp(ev: EepromValues) -> int:
    """HT40 TX-power compensation [SRC rt2800lib.c:4683-4725]. We only ever tune
    20 MHz channels, so CONFIG_CHANNEL_HT40 is never set ⇒ always 0. The HT40 arm
    is #TODO untestable (no 40 MHz path in this driver)."""
    return 0


def _get_gain_calibration_delta(t: RT3070Transport, ev: EepromValues) -> int:
    """TSSI-based temperature compensation, 2.4 GHz [SRC rt2800lib.c:4566-4681].

    Gated on EEPROM EXTERNAL_TX_ALC; bails to 0 when the TSSI reference / AGC step
    are unprogrammed (0xff) — which is this card (so BBP49 is never read). Ported;
    the per-step adjustment is #TODO untestable here (no TSSI table)."""
    if not ev.external_tx_alc:
        return 0
    tssi_bounds = [
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG1), C.EEPROM_TSSI_BOUND_BG1_MINUS4),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG1), C.EEPROM_TSSI_BOUND_BG1_MINUS3),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG2), C.EEPROM_TSSI_BOUND_BG2_MINUS2),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG2), C.EEPROM_TSSI_BOUND_BG2_MINUS1),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG3), C.EEPROM_TSSI_BOUND_BG3_REF),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG3), C.EEPROM_TSSI_BOUND_BG3_PLUS1),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG4), C.EEPROM_TSSI_BOUND_BG4_PLUS2),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG4), C.EEPROM_TSSI_BOUND_BG4_PLUS3),
        get_field(ev.word(C.EEPROM_TSSI_BOUND_BG5), C.EEPROM_TSSI_BOUND_BG5_PLUS4),
    ]
    step = get_field(ev.word(C.EEPROM_TSSI_BOUND_BG5), C.EEPROM_TSSI_BOUND_BG5_AGC_STEP)
    if tssi_bounds[4] == 0xFF or step == 0xFF:
        return 0
    # #TODO untestable: TSSI-programmed cards only — reads BBP49 + steps tx power.
    current_tssi = t.bbp_read(49)
    i = next((k for k in range(4) if current_tssi > tssi_bounds[k]), 4)
    if i == 4:
        i = next((k for k in range(8, 4, -1) if current_tssi < tssi_bounds[k]), 4)
    return (i - 4) * step


def _compensate_txpower(ev: EepromValues, is_rate_b: int, power_level: int,
                        txpower: int, delta: int) -> int:
    """Per-rate TX-power compensation [SRC rt2800lib.c:4748-4797]. RT3593/RT3883
    are different silicon (out of scope). With CAPABILITY_POWER_LIMIT unset (this
    card) the EIRP criterion collapses to reg_limit=0."""
    if ev.power_limit:
        # #TODO untestable: needs EEPROM EIRP criterion + runtime power_level;
        # this card has power_limit=False (EIRP_2G >= limit), so reg_limit=0.
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
    rt2800lib.c:2401-2438 rt2800_config_lna_gain]. EEPROM-cache read, no wire op.
    2.4 GHz uses EEPROM_LNA BG; the 5 GHz A0/A1/A2 arms are #TODO untestable."""
    if channel <= 14:
        return ev.lna_gain_bg
    return get_field(ev.word(C.EEPROM_LNA), 0xFF00)   # #TODO untestable: 5 GHz LNA_A0


def update_survey(t: RT3070Transport) -> None:
    """Snapshot channel idle/busy counters before a switch [SRC rt2800lib.c:1255-1264
    rt2800_update_survey]. Cleared-on-read, so issuing the reads is the point."""
    t.register_read(C.CH_IDLE_STA)
    t.register_read(C.CH_BUSY_STA)
    t.register_read(C.CH_BUSY_STA_SEC)


def config_ant(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """RX/TX antenna select via BBP1/BBP3 [SRC rt2800lib.c:2322-2398 rt2800_config_ant].
    1T1R takes TX_ANTENNA=0 / RX_ANTENNA=0; the 2T2R/3T3R and RT3572-BT arms are
    ported gated on chain count (#TODO untestable — no such hardware)."""
    r1 = t.bbp_read(1)
    r3 = t.bbp_read(3)

    if ev.tx_chain_num == 1:
        r1 = set_field(r1, C.BBP1_TX_ANTENNA, 0)
    else:
        r1 = set_field(r1, C.BBP1_TX_ANTENNA, 2)   # #TODO untestable: 2T2R/3T3R

    if ev.rx_chain_num == 1:
        if (chip.is_rt(C.RT3070) or chip.is_rt(C.RT3090)) and ev.ant_diversity:
            # default_ant.rx from ANT_DIVERSITY: 1/2 → ANTENNA_A, 3 → ANTENNA_B
            # [SRC rt2800lib.c:11251-11264]. Reference card has ANT_DIVERSITY=0 ⇒ skipped.
            _set_ant_diversity(t, rx_ant_a=ev.ant_diversity != 3)
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 0)
    elif ev.rx_chain_num == 2:
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 1)   # #TODO untestable: 2T2R
    else:
        r3 = set_field(r3, C.BBP3_RX_ANTENNA, 2)   # #TODO untestable: 3T3R

    t.bbp_write(3, r3)
    t.bbp_write(1, r1)


def _set_ant_diversity(t: RT3070Transport, rx_ant_a: bool) -> None:
    """SW antenna diversity for 1-chain RT3070/RT3090 with EEPROM ANT_DIVERSITY set
    [SRC rt2800lib.c:2300-2320 rt2800_set_ant_diversity], USB path. ``rx_ant_a`` is
    default_ant.rx == ANTENNA_A; it drives the MCU eesk pin (A→1) and the GPIO3 output
    bit (A→0). The PCI E2PROM_CSR arm is a different bus. Reference card has
    ANT_DIVERSITY=0, so config_ant never reaches this — runtime-gated, byte-identical."""
    eesk_pin = 1 if rx_ant_a else 0
    gpio_bit3 = 0 if rx_ant_a else 1
    t.mcu_request(C.MCU_ANT_SELECT, 0xFF, eesk_pin, 0)
    reg = t.register_read(C.GPIO_CTRL)
    reg = set_field(reg, C.GPIO_CTRL_DIR3, 0)
    reg = set_field(reg, C.GPIO_CTRL_VAL3, gpio_bit3)
    t.register_write(C.GPIO_CTRL, reg)


# ---------------------------------------------------------------------------
# Channel tune
# ---------------------------------------------------------------------------

def config_channel_rf3xxx(t: RT3070Transport, ev: EepromValues, drv: DrvData,
                          rf: tuple[int, int, int],
                          default_power1: int, default_power2: int) -> None:
    """RF3020/3021/3022 channel program [SRC rt2800lib.c:2547-2623
    rt2800_config_channel_rf3xxx]. ``rf`` is (rf1, rf2, rf3); calib_tx/rx come from
    the init RX-filter calibration (``calibration_bw20`` for our 20 MHz tunes)."""
    t.rfcsr_write(2, rf[0])

    rfcsr = t.rfcsr_read(3)
    rfcsr = set_field(rfcsr, C.RFCSR3_K, rf[2])
    t.rfcsr_write(3, rfcsr)

    rfcsr = t.rfcsr_read(6)
    rfcsr = set_field(rfcsr, C.RFCSR6_R1, rf[1])
    t.rfcsr_write(6, rfcsr)

    rfcsr = t.rfcsr_read(12)
    rfcsr = set_field(rfcsr, C.RFCSR12_TX_POWER, default_power1)
    t.rfcsr_write(12, rfcsr)

    rfcsr = t.rfcsr_read(13)
    rfcsr = set_field(rfcsr, C.RFCSR13_TX_POWER, default_power2)
    t.rfcsr_write(13, rfcsr)

    rfcsr = t.rfcsr_read(1)
    rfcsr = set_field(rfcsr, C.RFCSR1_RX0_PD, 0)
    rfcsr = set_field(rfcsr, C.RFCSR1_RX1_PD, int(ev.rx_chain_num <= 1))
    rfcsr = set_field(rfcsr, C.RFCSR1_RX2_PD, int(ev.rx_chain_num <= 2))
    rfcsr = set_field(rfcsr, C.RFCSR1_TX0_PD, 0)
    rfcsr = set_field(rfcsr, C.RFCSR1_TX1_PD, int(ev.tx_chain_num <= 1))
    rfcsr = set_field(rfcsr, C.RFCSR1_TX2_PD, int(ev.tx_chain_num <= 2))
    t.rfcsr_write(1, rfcsr)

    rfcsr = t.rfcsr_read(23)
    rfcsr = set_field(rfcsr, C.RFCSR23_FREQ_OFFSET, ev.freq_offset)
    t.rfcsr_write(23, rfcsr)

    # 20 MHz only ⇒ both TX and RX use calibration_bw20 [SRC 2594-2600].
    calib = drv.calibration_bw20
    rfcsr = t.rfcsr_read(24)
    rfcsr = set_field(rfcsr, C.RFCSR24_TX_CALIB, calib)
    t.rfcsr_write(24, rfcsr)

    rfcsr = t.rfcsr_read(31)
    rfcsr = set_field(rfcsr, C.RFCSR31_RX_CALIB, calib)
    t.rfcsr_write(31, rfcsr)

    rfcsr = t.rfcsr_read(7)
    rfcsr = set_field(rfcsr, C.RFCSR7_RF_TUNING, 1)
    t.rfcsr_write(7, rfcsr)

    rfcsr = t.rfcsr_read(30)
    rfcsr = set_field(rfcsr, C.RFCSR30_RF_CALIBRATION, 1)
    t.rfcsr_write(30, rfcsr)
    # kernel usleep_range(1000, 1500)
    rfcsr = set_field(rfcsr, C.RFCSR30_RF_CALIBRATION, 0)
    t.rfcsr_write(30, rfcsr)


def config_channel(t: RT3070Transport, chip: ChipInfo, ev: EepromValues,
                   drv: DrvData, channel: int, lna_gain: int) -> None:
    """Program the radio + baseband for ``channel`` [SRC rt2800lib.c:4161-4564
    rt2800_config_channel], RF3020 / 2.4 GHz / 1T1R path."""
    power1 = txpower_to_dev(channel, ev.power_byte(C.EEPROM_TXPOWER_BG1, channel - 1))
    power2 = txpower_to_dev(channel, ev.power_byte(C.EEPROM_TXPOWER_BG2, channel - 1))
    # tx_chain_num <= 2 here ⇒ no default_power3.

    rf = C.RF_VALS_3X_2G[channel]
    # config_channel RF dispatch [SRC rt2800lib.c:4185-4227]. The only tune this driver
    # ports is config_channel_rf3xxx — the radios RT3070/3071/3090 silicon ships (RF2020/
    # 3020/3021/3022/3320, eeprom.PORTED_RF_CHIPS). A runtime EEPROM whose NIC_CONF0.RF_TYPE
    # is outside that set (unburned/mislabeled 148f:3070, or the kernel's rf3052/rf3053/
    # rf3322/rf55xx radios this driver doesn't claim) is run on that same silicon-default
    # tune rather than -ENODEV'd like the kernel, so the card still comes up; driver._bringup
    # logs it once as an untested variant.
    config_channel_rf3xxx(t, ev, drv, rf, power1, power2)

    # RF3020 is NOT in the RF3070/RF3290/RF53xx VCO list, so the rfcsr30/rfcsr3
    # VCO-cal block [SRC 4228-4255] is not taken on this card.

    # BBP settings [SRC 4298-4306]: this card is the generic (non-RT3352/3593/3883/
    # 6352) arm — gain-track BBP62/63/64 and clear BBP86.
    t.bbp_write(62, 0x37 - lna_gain)
    t.bbp_write(63, 0x37 - lna_gain)
    t.bbp_write(64, 0x37 - lna_gain)
    t.bbp_write(86, 0x00)

    # channel <= 14 LNA tuning [SRC 4308-4326]; RT5390/5392/6352 are out of scope.
    if ev.external_lna_bg:
        t.bbp_write(82, 0x62)
        t.bbp_write(82, 0x62)        # written twice in the kernel source
        t.bbp_write(75, 0x46)
    else:
        t.bbp_write(82, 0x84)
        t.bbp_write(75, 0x50)

    reg = t.register_read(C.TX_BAND_CFG)
    reg = set_field(reg, C.TX_BAND_CFG_HT40_MINUS, 0)
    reg = set_field(reg, C.TX_BAND_CFG_A, int(channel > 14))
    reg = set_field(reg, C.TX_BAND_CFG_BG, int(channel <= 14))
    t.register_write(C.TX_BAND_CFG, reg)

    # TX_PIN_CFG: PA/LNA path enables [SRC 4360-4411]. tx_pin starts at 0 (non-RT6352).
    tx_pin = 0
    tx_pin = _config_tx_pin_pa(tx_pin, ev.tx_chain_num, channel)
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
    # RT2860 rev-C BBP69/70/73 tweak [SRC 4534-4544] is not this silicon.
    # kernel usleep_range(1000, 1500)

    # Clear channel-statistic counters [SRC 4548-4553].
    t.register_read(C.CH_IDLE_STA)
    t.register_read(C.CH_BUSY_STA)
    t.register_read(C.CH_BUSY_STA_SEC)
    # RT3352/RT5350 BBP49 update-flag clear [SRC 4558-4563] is out of scope.


def _config_tx_pin_pa(tx_pin: int, tx_chain_num: int, channel: int) -> int:
    """TX_PIN_CFG PA-enable switch [SRC rt2800lib.c:4363-4388]. 2T2R/3T3R secondary
    PAs (#TODO untestable) fall through to the primary like the kernel."""
    is_a = int(channel > 14)
    is_g = int(channel <= 14)
    if tx_chain_num >= 3:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A2_EN, is_a)   # #TODO untestable
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G2_EN, is_g)
    if tx_chain_num >= 2:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A1_EN, is_a)   # #TODO untestable
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G1_EN, is_g)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_A0_EN, is_a)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_PA_PE_G0_EN, is_g)       # no BT-coexist
    return tx_pin


def _config_tx_pin_lna(tx_pin: int, rx_chain_num: int) -> int:
    """TX_PIN_CFG LNA-enable switch [SRC rt2800lib.c:4390-4406]."""
    if rx_chain_num >= 3:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A2_EN, 1)     # #TODO untestable
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G2_EN, 1)
    if rx_chain_num >= 2:
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A1_EN, 1)     # #TODO untestable
        tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G1_EN, 1)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_A0_EN, 1)
    tx_pin = set_field(tx_pin, C.TX_PIN_CFG_LNA_PE_G0_EN, 1)
    return tx_pin


def config_txpower(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """Program the per-rate TX power [SRC rt2800lib.c:5338-5519
    rt2800_config_txpower_rt28xx], 2.4 GHz. RT3593/RT3883/RT6352 take other paths
    (different silicon, out of scope)."""
    delta = _get_txpower_bw_comp(ev)                      # 0 (20 MHz only)
    if (chip.is_rt(C.RT3070) or chip.is_rt(C.RT3071) or chip.is_rt(C.RT3090)
            or chip.is_rt(C.RT3572)):
        delta += _get_gain_calibration_delta(t, ev)      # 0 on this card (TSSI unprogrammed)
    # reg_delta = min(power_level - max_power, 0); monitor tunes to the channel's
    # regulatory max ⇒ power_level == max_power ⇒ 0 [SRC 4727-4746].
    delta += 0

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
        # RATE0..3 from byrate word i (is_rate_b only for the very first word),
        # RATE4..7 from word i+1.
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

def set_channel(t: RT3070Transport, chip: ChipInfo, ev: EepromValues,
                drv: DrvData, channel: int) -> None:
    """Tune to ``channel`` exactly as ``rt2x00mac_config(CHANGE_CHANNEL)`` does
    [SRC rt2x00mac.c:307-352, rt2x00config.c:193-280 + 104-163].

    Sequence (one stop / one start; the antenna reconfigure's refcounted
    ``start_queue`` is the single RX re-enable, the outer ``rt2x00mac_config`` start
    being a no-op):
        stop RX → [lna_gain → survey → config_channel → config_txpower] →
        reset_tuner → config_ant → reset_tuner → start RX
    """
    mac.stop_queue_rx(t)
    lna_gain = config_lna_gain(ev, channel)
    update_survey(t)
    config_channel(t, chip, ev, drv, channel, lna_gain)
    config_txpower(t, chip, ev)
    reset_tuner(t, chip, lna_gain)        # rt2x00lib_config (CHANGE_CHANNEL) tail
    config_ant(t, chip, ev)               # rt2x00lib_config_antenna
    reset_tuner(t, chip, lna_gain)        # config_antenna's reset_tuner(antenna=true)
    mac.start_queue_rx(t)                 # config_antenna's refcounted start_queue
