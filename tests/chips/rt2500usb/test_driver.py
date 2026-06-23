"""Unit tests for rt2500usb (Ralink RT2570) — no hardware.

A dict-backed FakeTransport exercises the register-access helpers so we
can assert init/BBP/RF/monitor-filter sequencing and RX/EEPROM decoding
without touching USB.

Fixtures are SYNTHETIC (fake MAC/BSSID/SSID) on purpose — we never bake a
real device's identifiers into the repo. The on-wire values these mirror
are verified separately against usb_dumps/captures_rt2500usb in
RT2500USB.md.
"""
from __future__ import annotations

import struct

from wifit3.chips.rt2500usb.bbp import eeprom_bbp_overrides, reset_tuner
from wifit3.chips.rt2500usb.chan import (
    antenna_defaults,
    config_ant,
    config_channel,
    rf_write,
)
from wifit3.chips.rt2500usb import monitor
from wifit3.chips.rt2500usb.constants import (
    ANTENNA_A,
    EEPROM_SIZE,
    MAC_CSR20,
    MAC_CSR20_ACTIVITY,
    MAC_CSR20_LINK,
    PHY_CSR7,
    MAC_CSR1,
    MAC_CSR1_HOST_READY,
    MAC_CSR9,
    MAC_CSR17,
    MAC_CSR17_BBP_CURR_STATE,
    MAC_CSR17_RF_CURR_STATE,
    PHY_CSR9,
    PHY_CSR10,
    RF2525E,
    STATE_AWAKE,
    TXRX_CSR1,
    TXRX_CSR1_AUTO_SEQUENCE,
    TXRX_CSR2,
    TXRX_CSR2_DROP_CRC,
    TXRX_CSR2_DROP_PHYSICAL,
    TXRX_CSR2_DROP_VERSION_ERROR,
    TXRX_CSR21,
    USB_DEVICE_MODE,
    USB_MODE_TEST,
    USB_SINGLE_WRITE,
)
from wifit3.chips.rt2500usb.driver import RT2500USBDriver
from wifit3.chips.rt2500usb.mac import (
    apply_monitor_filter,
    init_registers,
    set_state,
)
from wifit3.chips.rt2500usb.rx import parse_rx_urb
from wifit3.chips.rt2500usb.transport import get_field16, set_field16
from wifit3.chips.rt2500usb.tx import _tx_data_len, build_tx_desc, build_tx_urb


class FakeTransport:
    """16-bit register space backed by a dict, recording every write.

    ``regbusy_read`` never reports busy and MAC_CSR17 reads back with both
    current-state fields = AWAKE, so ``set_state(AWAKE)`` settles on the
    first poll.
    """

    def __init__(self, eeprom: bytes = b""):
        self.regs: dict[int, int] = {}
        self.writes: list = []
        self._eeprom = eeprom

    def read16(self, addr: int) -> int:
        if addr == MAC_CSR17:
            v = self.regs.get(addr, 0)
            v = set_field16(v, MAC_CSR17_BBP_CURR_STATE, STATE_AWAKE)
            v = set_field16(v, MAC_CSR17_RF_CURR_STATE, STATE_AWAKE)
            return v
        return self.regs.get(addr, 0)

    def write16(self, addr: int, val: int) -> None:
        self.regs[addr] = val & 0xFFFF
        self.writes.append((addr, val & 0xFFFF))

    def write16_mask(self, addr: int, mask: int, value: int) -> None:
        self.write16(addr, set_field16(self.read16(addr), mask, value) & 0xFFFF)

    def regbusy_read(self, addr: int, busy_mask: int):
        return True, self.read16(addr)

    def vendor_request_sw(self, request: int, offset: int, value: int) -> None:
        self.writes.append(("sw", request, offset, value))

    def read_eeprom(self, length: int = EEPROM_SIZE) -> bytes:
        return self._eeprom


def _synthetic_eeprom() -> bytes:
    """110-byte EEPROM with a fake MAC, RF2525E antenna word, rssi off 119."""
    ee = bytearray(b"\xff" * EEPROM_SIZE)
    # MAC at word 0x0002 (byte offset 4) — locally-administered fake.
    ee[4:10] = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x55])
    # ANTENNA word 0x000b (byte 0x16) = 0x2815 → RF_TYPE=5 (RF2525E), tx/rx=A.
    ee[0x16], ee[0x17] = 0x15, 0x28
    # CALIBRATE_OFFSET word 0x0036 (byte 0x6c) = 0x7977 → RSSI offset 0x77=119.
    ee[0x6c], ee[0x6d] = 0x77, 0x79
    # One BBP override word at 0x000e (byte 0x1c) = 0x1130 → BBP[17]=0x30.
    ee[0x1c], ee[0x1d] = 0x30, 0x11
    return bytes(ee)


def _synthetic_beacon() -> bytes:
    """A minimal, well-formed beacon with fake addresses."""
    fc = b"\x80\x00"
    dur = b"\x00\x00"
    da = b"\xff" * 6
    sa = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x55])
    bssid = sa
    seq = b"\x00\x00"
    body = (
        b"\x00" * 8            # timestamp
        + b"\x64\x00"          # beacon interval
        + b"\x11\x00"          # capability
        + b"\x00\x04test"      # SSID IE
        + b"\x01\x04\x82\x84\x8b\x96"  # supported rates IE
    )
    return fc + dur + da + sa + bssid + seq + body


# ----------------------------------------------------------------------
# Field helpers
# ----------------------------------------------------------------------
def test_field_helpers_roundtrip():
    reg = set_field16(0, 0x7F00, 17)
    reg = set_field16(reg, 0x8000, 1)
    assert reg == 0x9100
    assert get_field16(reg, 0x7F00) == 17
    assert get_field16(reg, 0x8000) == 1


# ----------------------------------------------------------------------
# EEPROM parse
# ----------------------------------------------------------------------
def test_parse_eeprom_synthetic():
    drv = RT2500USBDriver(dev=None)
    drv._parse_eeprom(_synthetic_eeprom())
    assert drv.mac_address == "02:11:22:33:44:55"
    assert drv.rf_type == RF2525E
    assert (drv._ant_tx, drv._ant_rx) == (ANTENNA_A, ANTENNA_A)
    assert drv._rssi_offset == 119


def test_antenna_defaults_sw_to_hw():
    # tx/rx fields both 0 (SW_DIVERSITY) → promoted to HW_DIVERSITY (3).
    assert antenna_defaults(0x0000) == (3, 3)
    # 0x2815 → tx=A, rx=A.
    assert antenna_defaults(0x2815) == (ANTENNA_A, ANTENNA_A)


def test_eeprom_bbp_overrides():
    overrides = eeprom_bbp_overrides(_synthetic_eeprom())
    assert (17, 0x30) in overrides


def _bbp_writes(t: "FakeTransport") -> dict[int, int]:
    """Decode the BBP register writes recorded on a FakeTransport — each
    bbp_write lands as a PHY_CSR7 write packing REG_ID<<8 | VALUE."""
    out: dict[int, int] = {}
    for addr, val in t.writes:
        if addr == PHY_CSR7:
            out[(val >> 8) & 0x7F] = val & 0xFF
    return out


def test_reset_tuner_calibrated_eeprom():
    # BBPTUNE words carry the calibrated bytes (this unit's real values):
    # R24=0x80, R25=0x50, R61=0x63, VGCUPPER=0x3b. reset_tuner seeds BBP
    # R24/R25/R61/R17 with them.
    ee = bytearray(_synthetic_eeprom())
    ee[0x31 * 2:0x31 * 2 + 2] = b"\x80\x68"   # BBPTUNE_R24 = 0x6880
    ee[0x32 * 2:0x32 * 2 + 2] = b"\x50\x38"   # BBPTUNE_R25 = 0x3850
    ee[0x33 * 2:0x33 * 2 + 2] = b"\x63\x73"   # BBPTUNE_R61 = 0x7363
    ee[0x34 * 2:0x34 * 2 + 2] = b"\x3b\xff"   # BBPTUNE_VGC = 0xff3b
    t = FakeTransport()
    reset_tuner(t, bytes(ee))
    assert _bbp_writes(t) == {24: 0x80, 25: 0x50, 61: 0x63, 17: 0x3B}


def test_reset_tuner_blank_eeprom_defaults():
    # All-0xff BBPTUNE words → the kernel's blank-EEPROM defaults.
    t = FakeTransport()
    reset_tuner(t, _synthetic_eeprom())       # synthetic EEPROM leaves 0x31-0x34 = 0xffff
    assert _bbp_writes(t) == {24: 0x40, 25: 0x40, 61: 0x60, 17: 0x40}


def _calibrated_eeprom() -> bytes:
    """Synthetic EEPROM with this unit's real BBP-tune words (R17 VGC = 0x3b)."""
    ee = bytearray(_synthetic_eeprom())
    ee[0x31 * 2:0x31 * 2 + 2] = b"\x80\x68"   # BBPTUNE_R24 = 0x6880
    ee[0x32 * 2:0x32 * 2 + 2] = b"\x50\x38"   # BBPTUNE_R25 = 0x3850
    ee[0x33 * 2:0x33 * 2 + 2] = b"\x63\x73"   # BBPTUNE_R61 = 0x7363
    ee[0x34 * 2:0x34 * 2 + 2] = b"\x3b\xff"   # BBPTUNE_VGC = 0xff3b
    return bytes(ee)


# ----------------------------------------------------------------------
# Monitor entry + per-hop tune (rt2x00 operational sequence)
# ----------------------------------------------------------------------
def test_tune_hop_reseeds_agc():
    # Every channel hop must re-seed BBP R17 (the VGC) via reset_tuner — the
    # kernel AGC behaviour. Regression guard: if a refactor drops
    # reset_tuner from the hop, this fails (not just the pcap gate).
    t = FakeTransport()
    monitor.tune_hop(t, RF2525E, 1, _calibrated_eeprom(), ANTENNA_A, ANTENNA_A)
    assert _bbp_writes(t)[17] == 0x3B


def test_enable_monitor_led_and_filter():
    t = FakeTransport()
    monitor.enable_monitor(t, RF2525E, _calibrated_eeprom(), ANTENNA_A, ANTENNA_A)
    # Radio + activity LEDs lit; monitor filter open with RX enabled.
    assert t.regs[MAC_CSR20] & (MAC_CSR20_LINK | MAC_CSR20_ACTIVITY)
    assert t.regs[TXRX_CSR2] == 0x46      # drop CRC/PLCP/version; accept ToDS, RX on


# ----------------------------------------------------------------------
# RX decode (RXD trails the frame)
# ----------------------------------------------------------------------
def test_parse_rx_urb_beacon():
    frame = _synthetic_beacon()
    fcs_pad = b"\x00\x00\x00\x00"         # hardware appends a 4-byte FCS;
                                          # parser strips it before yielding.
    on_air = frame + fcs_pad
    size = len(on_air)                    # DATABYTE_COUNT is the on-air len
    word0 = (size << 16)
    word1 = 0x50                          # RSSI raw 0x50 = 80
    rxd = struct.pack("<4I", word0, word1, 0, 0)
    urb = on_air + b"\x00" + rxd          # 1 alignment-pad byte before RXD

    rx = parse_rx_urb(urb, rssi_offset=120)
    assert rx is not None
    assert rx.mpdu == frame               # FCS stripped, MPDU body intact
    assert rx.rssi_dbm == 80 - 120        # -40 dBm
    assert rx.has_fcs_error is False
    assert rx.ofdm is False


def test_parse_rx_urb_crc_error_flagged():
    frame = _synthetic_beacon()
    word0 = (len(frame) << 16) | 0x20     # RXD_W0_CRC_ERROR
    rxd = struct.pack("<4I", word0, 0x40, 0, 0)
    rx = parse_rx_urb(frame + rxd)
    assert rx is not None and rx.has_fcs_error is True


def test_parse_rx_urb_too_short():
    assert parse_rx_urb(b"\x00" * 8) is None


# ----------------------------------------------------------------------
# Monitor filter
# ----------------------------------------------------------------------
def test_apply_monitor_filter_value():
    t = FakeTransport()
    apply_monitor_filter(t)
    # Accept real frames (accept bits clear); drop the CRC/PLCP/version error
    # classes the RX loop discards anyway.
    assert t.regs[TXRX_CSR2] == (
        TXRX_CSR2_DROP_CRC | TXRX_CSR2_DROP_PHYSICAL | TXRX_CSR2_DROP_VERSION_ERROR
    )
    assert t.regs[TXRX_CSR2] == 0x0046


# ----------------------------------------------------------------------
# init_registers
# ----------------------------------------------------------------------
def test_init_registers_sequence():
    t = FakeTransport()
    init_registers(t, revision=0x0005)    # rev nibble 5 ≥ VERSION_C

    # Prologue: USB test-mode + the magic single-write 0x0308 ← 0xf0.
    assert ("sw", USB_DEVICE_MODE, 0x0001, USB_MODE_TEST) in t.writes
    assert ("sw", USB_SINGLE_WRITE, 0x0308, 0x00F0) in t.writes
    # Fixed writes.
    assert (TXRX_CSR21, 0xE78F) in t.writes
    assert (MAC_CSR9, 0xFF1D) in t.writes
    # Final state: HOST_READY latched, AUTO_SEQUENCE set.
    assert t.regs[MAC_CSR1] & MAC_CSR1_HOST_READY
    assert t.regs[TXRX_CSR1] & TXRX_CSR1_AUTO_SEQUENCE


def test_set_state_settles():
    t = FakeTransport()
    # Should not raise (FakeTransport reports AWAKE current-state).
    set_state(t, STATE_AWAKE)


# ----------------------------------------------------------------------
# RF / channel encoding
# ----------------------------------------------------------------------
def test_rf_write_encoding():
    t = FakeTransport()
    rf_write(t, 2, 0x000008AA)
    assert (PHY_CSR9, 0x08AA) in t.writes
    # PHY_CSR10 = RF_BUSY(0x8000) | NUMBER_OF_BITS=20(0x1400) | high byte 0.
    assert (PHY_CSR10, 0x9400) in t.writes


def test_config_channel_2525e_halfband_first():
    t = FakeTransport()
    assert config_channel(t, RF2525E, 1, txpower=24) is True
    # The first RF write must be the half-band RF[2] = 0x000008aa.
    first_phy9 = next(v for (a, v) in t.writes if a == PHY_CSR9)
    assert first_phy9 == 0x08AA


def test_config_ant_runs_for_rf2525e():
    t = FakeTransport()
    # Should issue BBP (PHY_CSR7) writes + PHY_CSR5/6 writes without error.
    config_ant(t, RF2525E, ANTENNA_A, ANTENNA_A)
    assert t.writes  # at least some writes happened


# ----------------------------------------------------------------------
# TX descriptor build (vs capture deauth ground truth)
# ----------------------------------------------------------------------
def test_build_tx_desc_matches_capture_deauth():
    # capture-1 frame 9895: 26-byte deauth at 1 Mbps CCK, no ACK.
    txd = build_tx_desc(26, ack=False)
    assert struct.unpack("<5I", txd) == (0x001A10F0, 0x0000A580, 0x00F00400, 0, 0)


def test_build_tx_desc_plcp_length_scales():
    # PLCP length = (frame_len + 4 FCS) * 8 µs at 1 Mbps.
    txd = build_tx_desc(100, ack=False)
    word2 = struct.unpack("<5I", txd)[2]
    plcp_len = ((word2 >> 16) & 0xFF) | (((word2 >> 24) & 0xFF) << 8)
    assert plcp_len == (100 + 4) * 8


def test_build_tx_desc_ack_bit():
    no_ack = struct.unpack("<5I", build_tx_desc(26, ack=False))[0]
    with_ack = struct.unpack("<5I", build_tx_desc(26, ack=True))[0]
    assert not (no_ack & 0x200)      # TXD_W0_ACK clear
    assert with_ack & 0x200          # TXD_W0_ACK set


def test_tx_data_len_rules():
    assert _tx_data_len(46) == 46     # even, not a maxpacket multiple
    assert _tx_data_len(45) == 46     # odd → round up to even
    assert _tx_data_len(64) == 66     # exact maxpacket multiple → +2


def test_build_tx_urb_starts_with_desc():
    frame = _synthetic_beacon()
    urb = build_tx_urb(frame, ack=False)
    assert urb[:20] == build_tx_desc(len(frame), ack=False)
    assert urb[20:20 + len(frame)] == frame
    assert len(urb) % 2 == 0


# ----------------------------------------------------------------------
# Driver registration surface
# ----------------------------------------------------------------------
def test_driver_claims_nintendo_connector():
    ids = {(d.vid, d.pid) for d in RT2500USBDriver.SUPPORTED_IDS}
    assert (0x0411, 0x008B) in ids
    assert len(RT2500USBDriver.SUPPORTED_IDS) == 31
    assert RT2500USBDriver.SUPPORTED_CHANNELS == list(range(1, 15))


def test_driver_registered_in_manager():
    from wifit3.wlan.manager import _import_driver_classes
    assert RT2500USBDriver in _import_driver_classes().values()


class FakeUsbDev:
    """Records the last bulk write so we can assert inject_frame's path."""

    def __init__(self):
        self.last_write = None

    def write(self, ep, buf, timeout):
        self.last_write = (ep, bytes(buf))
        return len(buf)


async def test_driver_inject_frame_path():
    """Regression: driver.inject_frame forwards ``ack`` positionally through
    run_in_executor → tx.inject. A keyword-only ``ack`` broke this live."""
    drv = RT2500USBDriver(dev=FakeUsbDev())
    drv._bulk_out_ep = 0x01
    frame = _synthetic_beacon()

    sent_ok = await drv.inject_frame(frame, use_no_ack=True)
    assert sent_ok is True

    ep, buf = drv.dev.last_write
    assert ep == 0x01
    assert buf[:20] == build_tx_desc(len(frame), ack=False)
