"""rt2500usb MAC bring-up: revision read, warm probe, register init,
the MAC_CSR17 power-state handshake, and the always-monitor RX filter.

Port of rt2500usb.c:
  * rt2500usb_init_eeprom rev/RF identification (1434-1446) -> read_revision
  * rt2500usb_init_registers (766-879)                      -> init_registers
  * rt2500usb_set_state (981-1017)                          -> set_state
  * monitor-mode deviation (vs STA-mode config_filter)      -> apply_monitor_filter

The init sequence matches driver_captures/captures_rt2500usb/capture-2 frames
203-299 one-for-one (verified pre-port). RT2570 needs no firmware, so a
"warm" chip is simply one a prior session already initialised
(MAC_CSR1.HOST_READY latched).
"""
from __future__ import annotations

import logging
import time

from .constants import (
    CIPHER_NONE,
    DATA_FRAME_SIZE,
    IEEE80211_HEADER,
    MAC_CSR0,
    MAC_CSR1,
    MAC_CSR1_BBP_RESET,
    MAC_CSR1_HOST_READY,
    MAC_CSR1_SOFT_RESET,
    MAC_CSR8,
    MAC_CSR8_MAX_FRAME_UNIT,
    MAC_CSR9,
    MAC_CSR11,
    MAC_CSR13,
    MAC_CSR14,
    MAC_CSR15,
    MAC_CSR16,
    MAC_CSR17,
    MAC_CSR17_BBP_CURR_STATE,
    MAC_CSR17_BBP_DESIRE_STATE,
    MAC_CSR17_PUT_TO_SLEEP,
    MAC_CSR17_RF_CURR_STATE,
    MAC_CSR17_RF_DESIRE_STATE,
    MAC_CSR17_SET_STATE,
    MAC_CSR18,
    MAC_CSR18_AUTO_WAKE,
    MAC_CSR18_DELAY_AFTER_BEACON,
    MAC_CSR20,
    MAC_CSR20_ACTIVITY,
    MAC_CSR20_LINK,
    MAC_CSR22,
    PHY_CSR2,
    PHY_CSR2_LNA,
    PHY_CSR2_LNA_MODE,
    PHY_CSR4,
    PHY_CSR4_LOW_RF_LE,
    REGISTER_USB_BUSY_COUNT,
    RT2570_VERSION_C,
    STATE_AWAKE,
    TXRX_CSR0,
    TXRX_CSR0_ALGORITHM,
    TXRX_CSR0_IV_OFFSET,
    TXRX_CSR0_KEY_ID,
    TXRX_CSR1,
    TXRX_CSR1_AUTO_SEQUENCE,
    TXRX_CSR2,
    TXRX_CSR2_DISABLE_RX,
    TXRX_CSR2_DROP_BROADCAST,
    TXRX_CSR2_DROP_CONTROL,
    TXRX_CSR2_DROP_CRC,
    TXRX_CSR2_DROP_MULTICAST,
    TXRX_CSR2_DROP_NOT_TO_ME,
    TXRX_CSR2_DROP_PHYSICAL,
    TXRX_CSR2_DROP_TODS,
    TXRX_CSR2_DROP_VERSION_ERROR,
    TXRX_CSR5,
    TXRX_CSR6,
    TXRX_CSR7,
    TXRX_CSR8,
    TXRX_CSR_BBP_ID0,
    TXRX_CSR_BBP_ID0_VALID,
    TXRX_CSR_BBP_ID1,
    TXRX_CSR_BBP_ID1_VALID,
    TXRX_CSR19,
    TXRX_CSR19_BEACON_GEN,
    TXRX_CSR19_TBCN,
    TXRX_CSR19_TSF_COUNT,
    TXRX_CSR19_TSF_SYNC,
    TXRX_CSR21,
    USB_DEVICE_MODE,
    USB_MODE_TEST,
    USB_SINGLE_WRITE,
)
from .transport import RT2500USBTransport, get_field16, set_field16

logger = logging.getLogger(__name__)


def read_revision(t: RT2500USBTransport) -> int:
    """Read the RT2570 chip revision from MAC_CSR0 (rt2500usb.c:1440-1446).

    Validity per the kernel: ``(reg & 0xfff0) == 0`` and
    ``(reg & 0x000f) != 0``. Returns the full MAC_CSR0 value; the
    PHY_CSR2 version branch keys off the low nibble.
    """
    reg = t.read16(MAC_CSR0)
    if (reg & 0xFFF0) != 0 or (reg & 0x000F) == 0:
        raise IOError(f"Invalid RT2570 chip revision MAC_CSR0=0x{reg:04x}")
    return reg


def is_chip_warm(t: RT2500USBTransport) -> bool:
    """A prior session already initialised the MAC if MAC_CSR1.HOST_READY
    is latched. RT2570 has no firmware, so this is the only persistent
    "we already brought this up" signal. The driver pairs this with a
    bulk-IN smoke test before trusting it (M3)."""
    reg = t.read16(MAC_CSR1)
    return bool(get_field16(reg, MAC_CSR1_HOST_READY))


def set_state(t: RT2500USBTransport, state: int) -> None:
    """MAC_CSR17 power-state handshake (rt2500usb.c:981-1017).

    Writes the desired BBP/RF state, triggers SET_STATE, then polls
    until the current-state fields report ``state``. Raises on timeout.
    """
    put_to_sleep = 1 if state != STATE_AWAKE else 0

    reg = 0
    reg = set_field16(reg, MAC_CSR17_BBP_DESIRE_STATE, state)
    reg = set_field16(reg, MAC_CSR17_RF_DESIRE_STATE, state)
    reg = set_field16(reg, MAC_CSR17_PUT_TO_SLEEP, put_to_sleep)
    t.write16(MAC_CSR17, reg)
    reg = set_field16(reg, MAC_CSR17_SET_STATE, 1)
    t.write16(MAC_CSR17, reg)

    for _ in range(REGISTER_USB_BUSY_COUNT):
        reg2 = t.read16(MAC_CSR17)
        bbp_state = get_field16(reg2, MAC_CSR17_BBP_CURR_STATE)
        rf_state = get_field16(reg2, MAC_CSR17_RF_CURR_STATE)
        if bbp_state == state and rf_state == state:
            return
        t.write16(MAC_CSR17, reg)
        time.sleep(0.030)   # msleep(30)

    raise IOError(f"set_state({state}) timed out waiting for MAC_CSR17")


def init_registers(t: RT2500USBTransport, revision: int) -> None:
    """Port of rt2500usb_init_registers (rt2500usb.c:766-879).

    Linear CSR bring-up. Calls set_state(AWAKE) in the middle. ``revision``
    is the MAC_CSR0 value from read_revision() — its low nibble selects
    the PHY_CSR2 LNA branch.
    """
    # USB device test-mode + the magic single-byte write (770-773).
    t.vendor_request_sw(USB_DEVICE_MODE, 0x0001, USB_MODE_TEST)
    t.vendor_request_sw(USB_SINGLE_WRITE, 0x0308, 0x00F0)

    # Disable RX while we reconfigure (775-777).
    t.write16_mask(TXRX_CSR2, TXRX_CSR2_DISABLE_RX, 1)

    t.write16(MAC_CSR13, 0x1111)
    t.write16(MAC_CSR14, 0x1E11)

    # MAC soft-reset + BBP-reset pulse, then release (782-792).
    reg = t.read16(MAC_CSR1)
    reg = set_field16(reg, MAC_CSR1_SOFT_RESET, 1)
    reg = set_field16(reg, MAC_CSR1_BBP_RESET, 1)
    reg = set_field16(reg, MAC_CSR1_HOST_READY, 0)
    t.write16(MAC_CSR1, reg)

    reg = t.read16(MAC_CSR1)
    reg = set_field16(reg, MAC_CSR1_SOFT_RESET, 0)
    reg = set_field16(reg, MAC_CSR1_BBP_RESET, 0)
    reg = set_field16(reg, MAC_CSR1_HOST_READY, 0)
    t.write16(MAC_CSR1, reg)

    # TX BBP-id programming (794-820).
    reg = t.read16(TXRX_CSR5)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0, 13)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0_VALID, 1)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1, 12)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1_VALID, 1)
    t.write16(TXRX_CSR5, reg)

    reg = t.read16(TXRX_CSR6)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0, 10)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0_VALID, 1)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1, 11)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1_VALID, 1)
    t.write16(TXRX_CSR6, reg)

    reg = t.read16(TXRX_CSR7)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0, 7)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0_VALID, 1)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1, 6)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1_VALID, 1)
    t.write16(TXRX_CSR7, reg)

    reg = t.read16(TXRX_CSR8)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0, 5)
    reg = set_field16(reg, TXRX_CSR_BBP_ID0_VALID, 1)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1, 0)
    reg = set_field16(reg, TXRX_CSR_BBP_ID1_VALID, 0)
    t.write16(TXRX_CSR8, reg)

    # Clear synchronisation fields, preserving the rest (822-827).
    reg = t.read16(TXRX_CSR19)
    reg = set_field16(reg, TXRX_CSR19_TSF_COUNT, 0)
    reg = set_field16(reg, TXRX_CSR19_TSF_SYNC, 0)
    reg = set_field16(reg, TXRX_CSR19_TBCN, 0)
    reg = set_field16(reg, TXRX_CSR19_BEACON_GEN, 0)
    t.write16(TXRX_CSR19, reg)

    t.write16(TXRX_CSR21, 0xE78F)
    t.write16(MAC_CSR9, 0xFF1D)

    # Wake the BBP/RF (832 -> set_device_state(STATE_AWAKE)).
    set_state(t, STATE_AWAKE)

    # Now mark the host ready (835-839).
    reg = t.read16(MAC_CSR1)
    reg = set_field16(reg, MAC_CSR1_SOFT_RESET, 0)
    reg = set_field16(reg, MAC_CSR1_BBP_RESET, 0)
    reg = set_field16(reg, MAC_CSR1_HOST_READY, 1)
    t.write16(MAC_CSR1, reg)

    # LNA config — version-dependent (841-849). Our unit is rev>=C.
    if (revision & 0x000F) >= RT2570_VERSION_C:
        reg = t.read16(PHY_CSR2)
        reg = set_field16(reg, PHY_CSR2_LNA, 0)
    else:
        reg = 0
        reg = set_field16(reg, PHY_CSR2_LNA, 1)
        reg = set_field16(reg, PHY_CSR2_LNA_MODE, 3)
    t.write16(PHY_CSR2, reg)

    t.write16(MAC_CSR11, 0x0002)
    t.write16(MAC_CSR22, 0x0053)
    t.write16(MAC_CSR15, 0x01EE)
    t.write16(MAC_CSR16, 0x0000)

    # Max frame length = RX queue data size (856-859).
    reg = t.read16(MAC_CSR8)
    reg = set_field16(reg, MAC_CSR8_MAX_FRAME_UNIT, DATA_FRAME_SIZE)
    t.write16(MAC_CSR8, reg)

    # Security off (861-865).
    reg = t.read16(TXRX_CSR0)
    reg = set_field16(reg, TXRX_CSR0_ALGORITHM, CIPHER_NONE)
    reg = set_field16(reg, TXRX_CSR0_IV_OFFSET, IEEE80211_HEADER)
    reg = set_field16(reg, TXRX_CSR0_KEY_ID, 0)
    t.write16(TXRX_CSR0, reg)

    reg = t.read16(MAC_CSR18)
    reg = set_field16(reg, MAC_CSR18_DELAY_AFTER_BEACON, 90)
    t.write16(MAC_CSR18, reg)

    reg = t.read16(PHY_CSR4)
    reg = set_field16(reg, PHY_CSR4_LOW_RF_LE, 1)
    t.write16(PHY_CSR4, reg)

    reg = t.read16(TXRX_CSR1)
    reg = set_field16(reg, TXRX_CSR1_AUTO_SEQUENCE, 1)
    t.write16(TXRX_CSR1, reg)


def led_enable(t: RT2500USBTransport) -> None:
    """Turn the radio + activity LEDs on (rt2500usb_brightness_set, 264-281).

    The tail of rt2x00lib_enable_radio: ``led_radio(true)`` (RADIO/ASSOC →
    MAC_CSR20_LINK) then ``led_activity(true)`` (→ MAC_CSR20_ACTIVITY), each a
    read-modify-write of MAC_CSR20.
    """
    reg = t.read16(MAC_CSR20)
    reg = set_field16(reg, MAC_CSR20_LINK, 1)
    t.write16(MAC_CSR20, reg)
    reg = t.read16(MAC_CSR20)
    reg = set_field16(reg, MAC_CSR20_ACTIVITY, 1)
    t.write16(MAC_CSR20, reg)


def start_queue_rx(t: RT2500USBTransport) -> None:
    """Enable the RX queue — clear DISABLE_RX (rt2500usb_start_queue QID_RX,
    717-727). rt2x00 brackets every config with stop/start_queue(rx)."""
    t.write16_mask(TXRX_CSR2, TXRX_CSR2_DISABLE_RX, 0)


def stop_queue_rx(t: RT2500USBTransport) -> None:
    """Disable the RX queue — set DISABLE_RX (rt2500usb_stop_queue QID_RX,
    740-750). Antenna/channel changes are ignored unless RX is off first."""
    t.write16_mask(TXRX_CSR2, TXRX_CSR2_DISABLE_RX, 1)


def config_ps(t: RT2500USBTransport) -> None:
    """Power-save config, STATE_AWAKE path (rt2500usb_config_ps, 644-651).

    Monitor never sleeps: clear MAC_CSR18.AUTO_WAKE, then set_state(AWAKE).
    """
    reg = t.read16(MAC_CSR18)
    reg = set_field16(reg, MAC_CSR18_AUTO_WAKE, 0)
    t.write16(MAC_CSR18, reg)
    set_state(t, STATE_AWAKE)


def config_filter(t: RT2500USBTransport, monitoring: bool) -> None:
    """RX frame filter (rt2500usb_config_filter, 399-427).

    Driven by the mac80211 FIF_* flags airmon sets: FIF_FCSFAIL / FIF_PLCPFAIL
    OFF (so the chip drops CRC + PLCP errors — the RX loop discards them anyway,
    and on this full-speed bus surfacing them floods it), FIF_CONTROL +
    FIF_ALLMULTI ON, VERSION_ERROR always dropped, BROADCAST always accepted.
    ``monitoring`` clears DROP_NOT_TO_ME + DROP_TODS so client→AP (ToDS) frames
    from every BSS arrive. The resulting monitor value (0x0046) is exactly what
    the kernel's airmon path writes — this matches the wire, not a
    deviation. DISABLE_RX is owned by start/stop_queue_rx, not touched here.
    """
    reg = t.read16(TXRX_CSR2)
    reg = set_field16(reg, TXRX_CSR2_DROP_CRC, 1)            # !FIF_FCSFAIL
    reg = set_field16(reg, TXRX_CSR2_DROP_PHYSICAL, 1)       # !FIF_PLCPFAIL
    reg = set_field16(reg, TXRX_CSR2_DROP_CONTROL, 0)        # FIF_CONTROL
    reg = set_field16(reg, TXRX_CSR2_DROP_NOT_TO_ME, 0 if monitoring else 1)
    reg = set_field16(reg, TXRX_CSR2_DROP_TODS, 0 if monitoring else 1)
    reg = set_field16(reg, TXRX_CSR2_DROP_VERSION_ERROR, 1)
    reg = set_field16(reg, TXRX_CSR2_DROP_MULTICAST, 0)      # FIF_ALLMULTI
    reg = set_field16(reg, TXRX_CSR2_DROP_BROADCAST, 0)
    t.write16(TXRX_CSR2, reg)
    logger.debug("config_filter(monitoring=%s): TXRX_CSR2 = 0x%04x",
                 monitoring, reg)


def apply_monitor_filter(t: RT2500USBTransport) -> None:
    """Convenience: open the monitor RX filter and enable the RX queue.

    The bring-up calls config_filter / start_queue_rx in the kernel's
    bracketed order; this one-shot wrapper (TXRX_CSR2 = 0x0046, DISABLE_RX
    clear) is for the incremental HW-test phases and the post-warm reattach.
    """
    config_filter(t, monitoring=True)
    start_queue_rx(t)
