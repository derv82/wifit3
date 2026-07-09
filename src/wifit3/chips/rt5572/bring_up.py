"""The rt5572 cold register bring-up, in the kernel's exact wire order.

ONE source of truth, shared by ``driver.connect()`` (the live path) and
``scripts/rt5572/verify_pcap.py`` (the acceptance gate). Because both call THIS
function, the gate exercises exactly what connect() runs on hardware — there is
no second copy to drift. Change the sequence here and the gate re-tests it.

Order mirrors the RT5572/RF5592 cold-boot capture byte-for-byte:

    probe (MAC_CSR0) → EFUSE (autorun + 32-block loop, BEFORE firmware) →
    rfkill GPIO → xtal probe → firmware (autorun + blob + MCU boot) →
    radio-on (MCU_LED + MCU_WAKEUP) → USB-DMA + WPDMA wait →
    init_registers → prepare_bbp → init_bbp → init_rfcsr → enable_radio_finish

[SRC] data_dumps/rt2x00-source-v6.18/{rt2800usb.c,rt2800lib.c,rt2x00dev.c}:
rt2800_probe_hw → rt2x00lib_start → rt2800usb_enable_radio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .bbp import init_bbp, prepare_bbp
from .chan import is_xtal_40mhz
from .constants import (
    EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK,
    EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT,
    MCU_WAKEUP,
    RT_RT5592,
)
from .eeprom import EEPROM_OFFSET_FREQ, EepromValues, parse_eeprom, read_eeprom_efuse
from .firmware import load_firmware, load_firmware_blob, mcu_request
from .mac import (
    ChipId,
    enable_radio_finish,
    probe_hw_gpio,
    read_chip_id,
    set_radio_led,
    usb_enable_radio_dma,
)
from .mac import _wait_wpdma_ready as wait_wpdma_ready
from .reg_init import init_registers
from .rfcsr import init_rfcsr
from .transport import RT5572Transport

ProgressCb = Optional[Callable[[float, str], None]]


@dataclass
class BringUpState:
    """Everything connect() needs to keep after the cold bring-up."""

    chip: ChipId
    eeprom: EepromValues
    rf_cal: object
    xtal_40mhz: bool


def bring_up(t: RT5572Transport, *, progress: ProgressCb = None) -> BringUpState:
    """Run the full cold register bring-up against ``t`` and return the decoded state.

    Pure register I/O — no USB claim, no threads, no async — so both the live
    driver (via an executor) and the offline gate (via a ReplayDevice) drive the
    identical sequence."""
    def _p(pct: float, msg: str) -> None:
        if progress is not None:
            progress(pct, msg)

    chip = read_chip_id(t)                                   # MAC_CSR0: silicon id + rev
    sil = chip.silicon_id
    _p(0.10, "probe (MAC_CSR0)")

    ev = parse_eeprom(read_eeprom_efuse(t))                 # EFUSE — read at PROBE time
    _p(0.30, "EFUSE (MAC + LNA + freq cal)")

    probe_hw_gpio(t)                                        # rfkill GPIO dir (last probe-hw op)
    xtal = is_xtal_40mhz(t) if sil == RT_RT5592 else False  # probe_hw_mode: which crystal

    load_firmware(t, load_firmware_blob(), silicon_id=sil, progress_cb=None)
    _p(0.60, "firmware + MCU boot")

    # rt2x00lib_enable_radio → set_device_state(RADIO_ON) → rt2800usb_enable_radio.
    set_radio_led(t, ev.word(EEPROM_OFFSET_FREQ))          # leds-class radio LED on
    mcu_request(t, MCU_WAKEUP, token=0xFF, arg0=0, arg1=2)  # STATE_AWAKE
    usb_enable_radio_dma(t)                                 # USB_DMA_CFG bulk agg
    wait_wpdma_ready(t)                                    # rt2800_enable_radio prologue

    init_registers(t, sil)                                 # MAC config block
    _p(0.80, "init_registers")

    prepare_bbp(t)                                         # BBP/RF ready + MCU boot signal
    ant_div = (ev.nic_conf1 & EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK) \
        >> EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT
    init_bbp(t, sil, txpath=ev.txpath, rxpath=ev.rxpath,
             ant_diversity=ant_div, chip_rev=chip.revision)
    rf_cal = init_rfcsr(t, sil, freq_offset=ev.freq_offset, chip_rev=chip.revision,
                        txpath=ev.txpath, rxpath=ev.rxpath)

    enable_radio_finish(t, ev)                             # MAC/WPDMA enable + LED
    _p(1.00, "radio enabled")

    return BringUpState(chip=chip, eeprom=ev, rf_cal=rf_cal, xtal_40mhz=xtal)
