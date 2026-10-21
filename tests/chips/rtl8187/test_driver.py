"""M1 unit tests for the RTL8187L driver — chip-variant + warm probe.

Uses a tiny dict-backed mock transport so we can assert TX_CONF / CMD /
MAC decoding without touching hardware.
"""
from __future__ import annotations

from typing import Sequence

from wifit3.chips.rtl8187.constants import (
    CMD_RX_ENABLE,
    CMD_TX_ENABLE,
    HWVER_DEFAULT_NAME,
    REG_CMD,
    REG_TX_CONF,
    HwRev,
    TX_CONF_R8187vD,
    TX_CONF_R8187vD_B,
    USB_PID_RTL8187,
    USB_VID_REALTEK,
)
from wifit3.chips.rtl8187 import SUPPORTED_IDS
from wifit3.chips.rtl8187.driver import RTL8187Driver
from wifit3.chips.rtl8187.mac import (
    cold_bring_up,
    detect_chip_variant,
    is_chip_warm,
)


class FakeTransport:
    """Byte-addressable register space backed by a dict."""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}

    def write_bytes(self, addr: int, data: Sequence[int]) -> None:
        for i, b in enumerate(data):
            self.regs[addr + i] = b & 0xFF

    def _load(self, addr: int, n: int) -> int:
        out = 0
        for i in range(n):
            out |= self.regs.get(addr + i, 0) << (8 * i)
        return out

    def read8(self, addr: int, idx: int = 0) -> int:
        return self._load(addr, 1)

    def read32(self, addr: int, idx: int = 0) -> int:
        return self._load(addr, 4)

    def read_bytes(self, addr: int, length: int, idx: int = 0) -> bytes:
        return bytes(self.regs.get(addr + i, 0) for i in range(length))


def test_supported_ids_mirror_rtl8187_table_driver_info():
    from wifit3.device.manager import supported_ids

    rtl8187b = {(0x0BDA, 0x8189), (0x0BDA, 0x8197), (0x0BDA, 0x8198), (0x050D, 0x705E),
                (0x0846, 0x4260), (0x0DF6, 0x0028), (0x0DF6, 0x0029), (0x1737, 0x0073)}
    assert (USB_VID_REALTEK, USB_PID_RTL8187) == (0x0BDA, 0x8187)
    assert len(SUPPORTED_IDS) == 21                    # dev.c:43-80
    assert {(d.vid, d.pid) for d in SUPPORTED_IDS if d.chipset == "RTL8187B"} == rtl8187b
    assert all(d.chipset == "RTL8187L" for d in SUPPORTED_IDS if (d.vid, d.pid) not in rtl8187b)
    for vid, pid in rtl8187b:
        assert supported_ids()[(vid, pid)].key == "rtl8187"


def test_supported_channels_are_2g_only():
    # rtl818x_channels: 2.4 GHz 1..14; no 5 GHz radio.
    assert RTL8187Driver.SUPPORTED_CHANNELS == list(range(1, 15))


def test_detect_chip_variant_default_8187L_vB():
    """HWVER bits that don't match any known case fall through to the
    'RTL8187vB (default)' string from rtl8187_probe."""
    t = FakeTransport()
    # TX_CONF=0 → HWVER=0 → falls through to default.
    t.write_bytes(REG_TX_CONF, [0x00, 0x00, 0x00, 0x00])
    v = detect_chip_variant(t)
    assert v.name == HWVER_DEFAULT_NAME == "RTL8187vB"
    assert v.is_rtl8187b is False


def test_detect_chip_variant_8187vD():
    t = FakeTransport()
    # HWVER = R8187vD = 5 << 25 = 0x0A000000
    t.write_bytes(REG_TX_CONF, [0x00, 0x00, 0x00, (TX_CONF_R8187vD >> 24) & 0xFF])
    v = detect_chip_variant(t)
    assert v.name == "RTL8187vD"
    assert v.is_rtl8187b is False


def test_detect_chip_variant_early_8187B_masquerade():
    """HWVER = R8187vD_B → kernel sets priv->is_rtl8187b = 1, hw_rev = RTL8187BvB."""
    t = FakeTransport()
    # HWVER = R8187vD_B = 6 << 25 = 0x0C000000
    t.write_bytes(REG_TX_CONF, [0x00, 0x00, 0x00, (TX_CONF_R8187vD_B >> 24) & 0xFF])
    v = detect_chip_variant(t)
    assert v.is_rtl8187b is True
    assert v.hw_rev == HwRev.RTL8187BvB


def test_is_chip_warm_requires_both_tx_and_rx_enable():
    t = FakeTransport()

    # Cold: CMD = 0 → not warm.
    t.write_bytes(REG_CMD, [0x00])
    assert is_chip_warm(t) is False

    # TX only → not warm (matches the rtw88 / 8188eus invariant).
    t.write_bytes(REG_CMD, [CMD_TX_ENABLE])
    assert is_chip_warm(t) is False

    # RX only → not warm.
    t.write_bytes(REG_CMD, [CMD_RX_ENABLE])
    assert is_chip_warm(t) is False

    # Both → warm.
    t.write_bytes(REG_CMD, [CMD_TX_ENABLE | CMD_RX_ENABLE])
    assert is_chip_warm(t) is True


# ----------------------------------------------------------------------
# M2a bring-up tests
# ----------------------------------------------------------------------
class RecordingTransport(FakeTransport):
    """FakeTransport that also logs every read/write op for sequence
    assertions. Models HW that self-clears CMD_RESET on the first read
    (mirrors what the real chip does after cmd_reset's reset toggle).
    """

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple[str, int, int]] = []
        self._cmd_reset_seen = False

    def read8(self, addr: int, idx: int = 0) -> int:
        val = super().read8(addr, idx)
        # Simulate CMD_RESET self-clearing — without this cmd_reset's
        # 10-iter timeout loop would always raise.
        if addr == REG_CMD and val & 0x10:
            val &= ~0x10
            self.write_bytes(REG_CMD, [val])
        self.ops.append(("r8", addr, val))
        return val

    def read32(self, addr: int, idx: int = 0) -> int:
        val = super().read32(addr, idx)
        self.ops.append(("r32", addr, val))
        return val

    def write8(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w8", addr, val & 0xFF))
        self.write_bytes(addr, [val & 0xFF])

    def write16(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w16", addr, val & 0xFFFF))
        self.write_bytes(addr, [val & 0xFF, (val >> 8) & 0xFF])

    def write32(self, addr: int, val: int, idx: int = 0) -> None:
        self.ops.append(("w32", addr, val & 0xFFFFFFFF))
        self.write_bytes(
            addr,
            [val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF],
        )


def _read_rx_conf(t) -> int:
    from wifit3.chips.rtl8187.constants import REG_RX_CONF
    return (t.regs.get(REG_RX_CONF, 0) | (t.regs.get(REG_RX_CONF + 1, 0) << 8)
            | (t.regs.get(REG_RX_CONF + 2, 0) << 16) | (t.regs.get(REG_RX_CONF + 3, 0) << 24))


def test_start_writes_station_baseline_no_monitor(monkeypatch):
    """start() writes the kernel's station-mode RX_CONF baseline (dev.c:982-990) —
    0x9094FC0A, with NO monitor bit. Monitor mode is entered separately by
    configure_filter (the airmon path). Folding monitor into start() (an earlier port's
    bug) both mis-ordered the write and dropped the CTRL bit airmon also requests."""
    import wifit3.chips.rtl8187.mac as mac
    from wifit3.chips.rtl8187.constants import RX_CONF_MONITOR

    monkeypatch.setattr(mac.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    rx_conf = mac.start(t, False)
    assert rx_conf == 0x9094FC0A
    assert not (rx_conf & RX_CONF_MONITOR)
    assert _read_rx_conf(t) == 0x9094FC0A


def test_configure_filter_enters_monitor_with_ctrl(monkeypatch):
    """The monitor request (FIF_OTHER_BSS|FIF_CONTROL, all changed) on the start() baseline:
    0x9094FC0A ^ CTRL | MONITOR = 0x909CFC0B, total_flags = CONTROL|OTHER_BSS|ALLMULTI."""
    import wifit3.chips.rtl8187.mac as mac
    from wifit3.chips.rtl8187.constants import FIF_ALLMULTI, FIF_CONTROL, FIF_OTHER_BSS

    t = RecordingTransport()
    flags = FIF_OTHER_BSS | FIF_CONTROL
    rx_conf, total = mac.configure_filter(t, 0x9094FC0A, flags, flags, 0)
    assert rx_conf == 0x909CFC0B
    assert total == FIF_CONTROL | FIF_OTHER_BSS | FIF_ALLMULTI
    assert _read_rx_conf(t) == 0x909CFC0B


def test_configure_filter_xors_changed_flags_only(monkeypatch):
    """dev.c:1317-1325: FCS/CTRL toggle only on changed_flags; a reapply with CONTROL unchanged
    keeps CTRL, and dropping OTHER_BSS/ALLMULTI with no multicast clears MONITOR."""
    import wifit3.chips.rtl8187.mac as mac
    from wifit3.chips.rtl8187.constants import (
        FIF_ALLMULTI, FIF_CONTROL, FIF_FCSFAIL, FIF_OTHER_BSS, RX_CONF_FCS, RX_CONF_MONITOR,
    )

    t = RecordingTransport()
    flags = FIF_OTHER_BSS | FIF_CONTROL
    last_total = FIF_CONTROL | FIF_OTHER_BSS | FIF_ALLMULTI
    rx_conf, total = mac.configure_filter(t, 0x909CFC0B, last_total ^ flags, flags, 0)
    assert (rx_conf, total) == (0x909CFC0B, last_total)

    rx_conf, total = mac.configure_filter(t, 0x909CFC0B, FIF_FCSFAIL, FIF_FCSFAIL | flags, 0)
    assert rx_conf == 0x909CFC0B | RX_CONF_FCS
    assert total & FIF_FCSFAIL

    rx_conf, total = mac.configure_filter(t, 0x909CFC0B, 0, 0, 0)
    assert not rx_conf & RX_CONF_MONITOR
    rx_conf, _ = mac.configure_filter(t, 0x9094FC0A, 0, 0, 1)
    assert rx_conf & RX_CONF_MONITOR


def test_cold_bring_up_latches_cmd_tx_rx_enable(monkeypatch):
    """cold_bring_up's contract: after it returns, CMD has both
    TX_ENABLE and RX_ENABLE bits set. This is what the hw demo asserts."""
    import wifit3.chips.rtl8187.mac as mac

    # Make the kernel msleep()s instant — we don't want unit tests to
    # spend 500ms on real sleeps.
    monkeypatch.setattr(mac.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    # EEPROM_CMD self-clears CONFIG bit on read (mirrors HW auto-load).
    # Without this cmd_reset's EEPROM-wait loop would time out.
    real_read8 = t.read8

    def patched_read8(addr: int, idx: int = 0) -> int:
        val = real_read8(addr, idx)
        if addr == 0xFF50 and val & 0xC0:
            t.write_bytes(0xFF50, [val & ~0xC0])
        return val

    t.read8 = patched_read8  # type: ignore[method-assign]

    cold_bring_up(t, rf_init=lambda _t: None, is_rtl8187b=False)

    cmd = t.regs.get(REG_CMD, 0)
    assert cmd & CMD_TX_ENABLE, f"CMD={cmd:#04x} missing TX_ENABLE"
    assert cmd & CMD_RX_ENABLE, f"CMD={cmd:#04x} missing RX_ENABLE"


# ----------------------------------------------------------------------
# M3 RX descriptor tests
# ----------------------------------------------------------------------
def _build_rx_urb(frame_body: bytes, *, agc: int = 0x40, crc_err: bool = False,
                  rate_idx: int = 4, mac_time: int = 0x1122334455667788) -> bytes:
    """Build a synthetic bulk-IN URB: frame + 4B FCS + 16B trailer."""
    import struct
    fcs = b"\xaa\xbb\xcc\xdd"
    frame_with_fcs = frame_body + fcs
    flags = len(frame_with_fcs) & 0x0FFF
    flags |= (rate_idx & 0xF) << 20
    if crc_err:
        flags |= 1 << 13
    trailer = struct.pack(
        "<IBBBBQ",
        flags,
        0x00,         # noise
        0x80,         # signal (bit 7 = antenna B)
        agc & 0xFF,
        0x00,         # reserved
        mac_time,
    )
    return frame_with_fcs + trailer


def test_parse_rx_urb_decodes_trailer_and_strips_fcs():
    from wifit3.chips.rtl8187.rx import parse_rx_urb

    body = b"\x80\x00" + b"\x00" * 22 + b"BEACON"  # 30-byte body
    urb = _build_rx_urb(body, agc=0x40)
    rx = parse_rx_urb(urb, False)
    assert rx is not None
    # FCS stripped → MPDU == body.
    assert rx.mpdu == body
    # antenna bit 7 of signal=0x80 → 1.
    assert rx.antenna == 1
    # rate_idx round-trips.
    assert rx.rate_idx == 4
    # RSSI: -4 - ((27 * 64) >> 6) = -4 - 27 = -31
    assert rx.rssi_dbm == -31
    assert rx.has_fcs_error is False
    assert rx.mac_time == 0x1122334455667788


def test_parse_rx_urb_rssi_scales_linearly_with_agc():
    """The kernel formula `-4 - ((27 * agc) >> 6)` is a step-linear
    function of agc. Spot-check a few values to lock the math down."""
    from wifit3.chips.rtl8187.rx import parse_rx_urb

    body = b"\x00" * 30
    for agc, expected in [(0, -4), (8, -7), (64, -31), (128, -58), (255, -111)]:
        rx = parse_rx_urb(_build_rx_urb(body, agc=agc), False)
        assert rx is not None
        assert rx.rssi_dbm == expected, f"agc=0x{agc:02x}: got {rx.rssi_dbm}"


def test_parse_rx_urb_returns_none_on_short_buffer():
    from wifit3.chips.rtl8187.rx import parse_rx_urb

    assert parse_rx_urb(b"", False) is None
    assert parse_rx_urb(b"\x00" * 15, False) is None  # 1 byte short of the trailer


def test_parse_rx_urb_returns_none_on_oversized_frame_len():
    """If the trailer claims a frame longer than the URB can hold, drop."""
    import struct
    from wifit3.chips.rtl8187.rx import parse_rx_urb

    # 16-byte URB = just a trailer, no body. But trailer claims a 500-byte frame.
    trailer = struct.pack("<IBBBBQ", 500, 0, 0, 0x40, 0, 0)
    assert parse_rx_urb(trailer, False) is None


def test_parse_rx_urb_flags_fcs_error():
    from wifit3.chips.rtl8187.rx import parse_rx_urb

    body = b"\x00" * 30
    rx = parse_rx_urb(_build_rx_urb(body, crc_err=True), False)
    assert rx is not None
    assert rx.has_fcs_error is True


# ----------------------------------------------------------------------
# M4 set_channel tests
# ----------------------------------------------------------------------
def _zero_power():
    from wifit3.chips.rtl8187.rtl8225 import TxPower
    return TxPower(hw_value=tuple([0] * 14), base=0)


def test_config_channel_rejects_out_of_range(monkeypatch):
    from wifit3.chips.rtl8187 import chan as chan_mod
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant
    import pytest

    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    for bad in (0, 15, 100, -1):
        with pytest.raises(ValueError):
            chan_mod.config_channel(t, setup=RfSetup(asic_rev=1, variant=RfVariant.RTL8225, hw_rev=None),
                                    channel=bad, power=_zero_power())


def test_config_channel_writes_correct_rf7_word(monkeypatch):
    """config_channel(ch) must write RF reg 0x7 = rtl8225_chan[ch-1] (the synth word).

    Spot-check a few channels — kernel table is shared across BCD + z2."""
    from wifit3.chips.rtl8187 import chan as chan_mod
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, rtl8225_chan

    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    rf_writes: list[tuple[int, int]] = []

    def fake_write(t, addr, data, asic_rev):
        rf_writes.append((addr, data))

    monkeypatch.setattr(chan_mod, "rtl8225_write", fake_write)
    # set_tx_power also runs — patch it to a no-op to keep the test focused on the synth.
    monkeypatch.setattr(chan_mod, "set_tx_power", lambda *a, **kw: None)

    t = RecordingTransport()
    for ch in (1, 6, 11, 13, 14):
        rf_writes.clear()
        chan_mod.config_channel(t, setup=RfSetup(asic_rev=1, variant=RfVariant.RTL8225, hw_rev=None),
                                channel=ch, power=_zero_power())
        # Exactly one RF write — RF reg 0x7 with the synth word.
        assert rf_writes == [(0x7, rtl8225_chan[ch - 1])], (
            f"ch={ch}: expected single RF7 write with 0x{rtl8225_chan[ch-1]:03x}, "
            f"got {rf_writes}"
        )


def test_config_channel_brackets_tune_in_tx_conf_loopback(monkeypatch):
    """config_channel mirrors rtl8187_config: read TX_CONF, OR in LOOPBACK_MAC, retune,
    restore TX_CONF, then write the 4 ATIM/beacon interval registers (dev.c:1162-1176)."""
    from wifit3.chips.rtl8187 import chan as chan_mod
    from wifit3.chips.rtl8187.constants import (
        REG_ATIM_WND, REG_ATIMTR_INTERVAL, REG_BEACON_INTERVAL,
        REG_BEACON_INTERVAL_TIME, REG_TX_CONF, TX_CONF_LOOPBACK_MAC,
    )
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant

    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "rtl8225_write", lambda *a, **kw: None)
    monkeypatch.setattr(chan_mod, "set_tx_power", lambda *a, **kw: None)

    t = RecordingTransport()
    t.write_bytes(REG_TX_CONF, [0x00, 0x00, 0xe8, 0x98])   # read-back base (HWVER bits set)
    chan_mod.config_channel(t, setup=RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None), channel=1,
                            power=_zero_power())

    tx_writes = [op for op in t.ops if op[0] == "w32" and op[1] == REG_TX_CONF]
    assert tx_writes == [
        ("w32", REG_TX_CONF, 0x98e80000 | TX_CONF_LOOPBACK_MAC),  # loopback on
        ("w32", REG_TX_CONF, 0x98e80000),                         # restore
    ]
    atim = [op for op in t.ops if op[0] == "w16"
            and op[1] in (REG_ATIM_WND, REG_ATIMTR_INTERVAL,
                          REG_BEACON_INTERVAL, REG_BEACON_INTERVAL_TIME)]
    assert atim == [
        ("w16", REG_ATIM_WND, 2),
        ("w16", REG_ATIMTR_INTERVAL, 100),
        ("w16", REG_BEACON_INTERVAL, 100),
        ("w16", REG_BEACON_INTERVAL_TIME, 100),
    ]


# ----------------------------------------------------------------------
# M5 TX tests
# ----------------------------------------------------------------------
def test_build_tx_hdr_default_shape():
    from wifit3.chips.rtl8187.tx import TX_HDR_SIZE, build_tx_hdr
    import struct

    hdr = build_tx_hdr(100)  # 1 Mbps CCK, retry=7, no morefrag
    assert len(hdr) == TX_HDR_SIZE == 12

    flags, rts_dur, length_field, retry = struct.unpack("<IHHI", hdr)
    # frame length in low 12 bits
    assert flags & 0x0FFF == 100
    # NO_ENC bit (15) set, no other high bits
    assert flags & (1 << 15)
    # rate hw_value = 0 (1 Mbps CCK)
    assert (flags >> 24) & 0xF == 0
    # rts_duration = 0
    assert rts_dur == 0
    # kernel writes len=0 (frame length is in flags, NOT this field)
    assert length_field == 0
    # retry = (RETRY_COUNT-1) << 8 = 6 << 8 = 0x0600
    assert retry == 0x0600


def test_build_tx_hdr_rate_index_lands_in_bits_24_27():
    from wifit3.chips.rtl8187.tx import RATE_24MBPS_OFDM, build_tx_hdr
    import struct

    hdr = build_tx_hdr(50, rate_hw_value=RATE_24MBPS_OFDM)
    flags, *_ = struct.unpack("<IHHI", hdr)
    assert (flags >> 24) & 0xF == RATE_24MBPS_OFDM == 8


def test_build_tx_hdr_rejects_oversized_frame():
    from wifit3.chips.rtl8187.tx import build_tx_hdr
    import pytest

    with pytest.raises(ValueError):
        build_tx_hdr(0x1000)  # 4096 doesn't fit in 12 bits
    with pytest.raises(ValueError):
        build_tx_hdr(0)


def test_stamp_seq_ctrl_increments_and_preserves_frag():
    """The 8187L has no hardware seq assignment, so inject stamps an incrementing 802.11
    sequence number (step 0x10, the number lives in seq_ctrl bits [4:15]) while preserving
    the fragment bits — else every injected frame is seq=0 and an AP dedups our
    association/EAPOL conversation (PMKID extraction / WPS). Mirrors rtl8187_tx (dev.c)."""
    from wifit3.chips.rtl8187.tx import stamp_seq_ctrl

    f = bytearray(26)                       # deauth-sized, frag 0
    seqno = stamp_seq_ctrl(f, 0)
    assert seqno == 0x10
    assert (f[22] | (f[23] << 8)) == 0x0010
    seqno = stamp_seq_ctrl(bytearray(26), seqno)
    assert seqno == 0x20                    # next frame advances one sequence

    # A fragment burst shares one sequence; only frag==0 advances the counter.
    f0, f1, f2 = bytearray(30), bytearray(30), bytearray(30)
    f0[22], f1[22], f2[22] = 0x00, 0x01, 0x02
    s = stamp_seq_ctrl(f0, 0x20)
    assert s == 0x30 and (f0[22] | (f0[23] << 8)) == 0x0030
    s = stamp_seq_ctrl(f1, s)
    assert s == 0x30 and (f1[22] | (f1[23] << 8)) == 0x0031   # same seq, frag 1
    s = stamp_seq_ctrl(f2, s)
    assert s == 0x30 and (f2[22] | (f2[23] << 8)) == 0x0032

    # Control frames (< 24 B) carry no seq_ctrl — untouched.
    assert stamp_seq_ctrl(bytearray(10), 0x30) == 0x30
    # 12-bit sequence wraps at 0xFFF0.
    assert stamp_seq_ctrl(bytearray(26), 0xFFF0) == 0x0000


def test_build_deauth_structure():
    from wifit3.chips.rtl8187.tx import (
        BROADCAST_MAC,
        DEAUTH_REASON_CLASS3,
        build_deauth,
    )
    bssid = bytes.fromhex("aabbccddeeff")
    f = build_deauth(BROADCAST_MAC, bssid)
    assert len(f) == 26
    # Frame Control = 0xC0 0x00 (mgmt, deauth)
    assert f[0] == 0xC0
    assert f[1] == 0x00
    # Duration/NAV = 0 for a broadcast target (group-addressed frames are not ACKed)
    assert f[2:4] == b"\x00\x00"
    # addr1 = target (broadcast)
    assert f[4:10] == BROADCAST_MAC
    # addr2 = src (defaults to bssid)
    assert f[10:16] == bssid
    # addr3 = bssid
    assert f[16:22] == bssid
    # Reason code (LE) = CLASS3 = 7
    assert f[24] == DEAUTH_REASON_CLASS3
    assert f[25] == 0


def test_build_deauth_unicast_sets_ack_nav():
    """A unicast target gets the ACK NAV (0x013A) in duration; broadcast gets 0. Matches
    aireplay-ng: the addressed STA ACKs, so we reserve SIFS + a 1 Mbps ACK for it."""
    from wifit3.chips.rtl8187.tx import build_deauth

    bssid = bytes.fromhex("aabbccddeeff")
    client = bytes.fromhex("001122334455")   # unicast (even first octet)
    f = build_deauth(client, bssid)
    # Duration = 0x013A little-endian (the unicast-ACK NAV)
    assert f[2:4] == b"\x3a\x01"


def test_config_channel_dispatches_z2_set_tx_power(monkeypatch):
    """Variant=RTL8225Z2 must route through rtl8225z2_rf_set_tx_power, NOT the BCD one —
    matches kernel rtl8225_rf_set_channel dispatch. The shared set_tx_power dispatcher
    (rtl8225.set_tx_power) picks the variant from the EEPROM hw_value."""
    from wifit3.chips.rtl8187 import chan as chan_mod
    import wifit3.chips.rtl8187.rtl8225 as rf
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant

    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    called = {"bcd": 0, "z2": 0}
    monkeypatch.setattr(rf, "rtl8225_rf_set_tx_power",
                        lambda *a, **kw: called.__setitem__("bcd", called["bcd"] + 1))
    monkeypatch.setattr(rf, "rtl8225z2_rf_set_tx_power",
                        lambda *a, **kw: called.__setitem__("z2", called["z2"] + 1))
    monkeypatch.setattr(chan_mod, "rtl8225_write", lambda *a, **kw: None)

    t = RecordingTransport()

    chan_mod.config_channel(t, setup=RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None), channel=6,
                            power=_zero_power())
    assert called == {"bcd": 0, "z2": 1}

    chan_mod.config_channel(t, setup=RfSetup(asic_rev=1, variant=RfVariant.RTL8225, hw_rev=None), channel=6,
                            power=_zero_power())
    assert called == {"bcd": 1, "z2": 1}


def test_z2_set_tx_power_uses_eeprom_cck_ofdm_gain(monkeypatch):
    """The AWUS036H EEPROM (ch1 hw_value=0x55, txpwr_base=0x36) must yield the calibrated
    TX_GAIN_CCK=0x0b / TX_GAIN_OFDM=0x12 the kernel writes on the wire:
    cck=min(5,15)+(0x36&0xF)=11, ofdm=(5+10)+(0x36>>4)=18 — both direct-index the z2 gain
    table. The pre-EEPROM stub (cck=ofdm=0) wrote 0x00/0x03 instead; this is what made the
    channel hops diverge from the capture until the 93cx6 read landed."""
    import wifit3.chips.rtl8187.rtl8225 as rf
    from wifit3.chips.rtl8187.constants import REG_TX_GAIN_CCK, REG_TX_GAIN_OFDM
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, TxPower, set_tx_power

    monkeypatch.setattr(rf.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    power = TxPower(hw_value=tuple([0x55] * 14), base=0x36)
    set_tx_power(t, RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None), 1, power)
    assert t.regs.get(REG_TX_GAIN_CCK) == 0x0B
    assert t.regs.get(REG_TX_GAIN_OFDM) == 0x12


def test_set_anaparam_brackets_with_eeprom_config_normal(monkeypatch):
    """set_anaparam's contract: writes ANAPARAM + ANAPARAM2 inside an
    EEPROM_CMD CONFIG→NORMAL bracket, with CONFIG3 ANAPARAM_WRITE bit
    set during the window."""
    import wifit3.chips.rtl8187.mac as mac

    monkeypatch.setattr(mac.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    mac.set_anaparam(t, rfon=True, is_rtl8187b=False)

    # First op opens the analog write window; last op closes it.
    assert t.ops[0] == ("w8", 0xFF50, 0xC0)   # EEPROM_CMD = CONFIG (3<<6)
    assert t.ops[-1] == ("w8", 0xFF50, 0x00)  # EEPROM_CMD = NORMAL (0<<6)

    # ANAPARAM + ANAPARAM2 writes used the *_ON constants.
    anaparam_ops = [op for op in t.ops if op[0] == "w32"]
    assert anaparam_ops == [
        ("w32", 0xFF54, 0xA0000A59),
        ("w32", 0xFF60, 0x860C7312),
    ]


def test_stop_matches_rtl8187_stop_op_order(monkeypatch):
    """rtl8187_stop (dev.c:1022-1053): INT_MASK=0, CMD &= ~(TX|RX), rf->stop, ANAPARAM off,
    CONFIG4 |= VCOOFF inside an EEPROM_CMD CONFIG/NORMAL window."""
    import wifit3.chips.rtl8187.mac as mac
    from wifit3.chips.rtl8187.constants import (
        ANAPARAM2_OFF, ANAPARAM_OFF, REG_ANAPARAM, REG_ANAPARAM2, REG_CONFIG3, REG_CONFIG4,
        REG_EEPROM_CMD, REG_INT_MASK,
    )

    t = RecordingTransport()
    t.write_bytes(REG_CMD, [0x0C | 0x02])
    t.write_bytes(REG_CONFIG3, [0x44])
    t.write_bytes(REG_CONFIG4, [0x01])
    mac.stop(t, lambda _t: t.ops.append(("rf_stop", 0, 0)), False)

    assert t.ops == [
        ("w16", REG_INT_MASK, 0),
        ("r8", REG_CMD, 0x0E),
        ("w8", REG_CMD, 0x02),
        ("rf_stop", 0, 0),
        ("w8", REG_EEPROM_CMD, 0xC0),
        ("r8", REG_CONFIG3, 0x44),
        ("w8", REG_CONFIG3, 0x44),
        ("w32", REG_ANAPARAM, ANAPARAM_OFF),
        ("w32", REG_ANAPARAM2, ANAPARAM2_OFF),
        ("w8", REG_CONFIG3, 0x04),
        ("w8", REG_EEPROM_CMD, 0x00),
        ("w8", REG_EEPROM_CMD, 0xC0),
        ("r8", REG_CONFIG4, 0x01),
        ("w8", REG_CONFIG4, 0x81),
        ("w8", REG_EEPROM_CMD, 0x00),
    ]


def test_rtl8225_rf_stop_writes_rf4(monkeypatch):
    import wifit3.chips.rtl8187.rtl8225 as rf

    writes: list[tuple[int, int, int]] = []
    monkeypatch.setattr(rf, "rtl8225_write",
                        lambda t, addr, data, asic_rev: writes.append((addr, data, asic_rev)))
    rf.rtl8225_rf_stop(object(), 1)
    assert writes == [(0x4, 0x1F, 1)]


def _probe_with_fake_eeprom(monkeypatch, tx_conf_top: int, word_value: int = 0x1234,
                            product_id: int = 0x8187, is_rtl8187b: bool = False):
    """Run probe() with the EEPROM, rfkill and RF SPI mocked; return (result, eeprom words, rf writes)."""
    import wifit3.chips.rtl8187.probe as probe_mod
    import wifit3.chips.rtl8187.rtl8225 as rf

    words: list[int] = []

    class FakeEeprom:
        def __init__(self, t, width):
            pass

        def read(self, word: int) -> int:
            words.append(word)
            return word_value

        def multiread(self, word: int, n: int) -> list[int]:
            return [self.read(word + i) for i in range(n)]

    rf_writes: list[tuple[int, int]] = []
    monkeypatch.setattr(probe_mod, "Eeprom93cx6", FakeEeprom)
    monkeypatch.setattr(probe_mod, "eeprom_width", lambda t: 6)
    monkeypatch.setattr(probe_mod, "is_radio_enabled", lambda t, mask: True)
    monkeypatch.setattr(rf, "rtl8225_write", lambda t, a, d, r: rf_writes.append((a, d)))
    monkeypatch.setattr(rf, "rtl8225_read", lambda t, a: {8: 0x588, 9: 0x700}[a])

    t = RecordingTransport()
    t.write_bytes(REG_TX_CONF, [0x00, 0x00, 0x00, tx_conf_top])
    return probe_mod.probe(t, product_id, is_rtl8187b), words, rf_writes


def test_probe_8187L_reads_channel_11_14_words_and_probes_rf(monkeypatch):
    from wifit3.chips.rtl8187.rtl8225 import RfVariant

    pr, words, rf_writes = _probe_with_fake_eeprom(monkeypatch, 0x00)
    assert pr.rfkill_off is True
    assert words[-3:] == [0x1B, 0x1C, 0x3F]            # CHAN_6 + 1, then LED custid
    assert rf_writes == [(0, 0x1B7), (0, 0x0B7)]
    assert pr.setup.variant is RfVariant.RTL8225Z2
    assert pr.power.hw_value[10:] == (0x34, 0x12, 0x34, 0x12)


def test_probe_early_8187B_takes_the_8187B_tail(monkeypatch):
    """dev.c:1592-1601: ch11 from CHAN_6 low byte, ch12 from word 0x0A, ch13/14 from 0x1C;
    rtl8187_detect_rf returns z2_b without the RF SPI probe."""
    from wifit3.chips.rtl8187.rtl8225 import RfVariant

    pr, words, rf_writes = _probe_with_fake_eeprom(monkeypatch, (TX_CONF_R8187vD_B >> 24) & 0xFF)
    assert words[-4:] == [0x1B, 0x0A, 0x1C, 0x3F]
    assert rf_writes == []
    assert pr.chip.is_rtl8187b and pr.chip.hw_rev == HwRev.RTL8187BvB
    assert pr.setup.variant is RfVariant.RTL8225Z2_B
    assert pr.power.hw_value[10:] == (0x34, 0x34, 0x34, 0x12)


def test_z2_set_tx_power_wraps_ofdm_power_as_u8(monkeypatch):
    """rtl8225.c:637: u8 ofdm_power += txpwr_base >> 4 wraps. base=0xFFFF, hw_value=0x55:
    ofdm = (15 + 0xFFF) & 0xFF = 14, not min(4110, 35) = 35; cck = 5 + 15 = 20."""
    import wifit3.chips.rtl8187.rtl8225 as rf
    from wifit3.chips.rtl8187.constants import REG_TX_GAIN_CCK, REG_TX_GAIN_OFDM
    from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, TxPower, set_tx_power

    monkeypatch.setattr(rf.time, "sleep", lambda *_a, **_kw: None)

    t = RecordingTransport()
    set_tx_power(t, RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None), 1, TxPower(hw_value=tuple([0x55] * 14), base=0xFFFF))
    assert t.regs.get(REG_TX_GAIN_OFDM) == 0x0E
    assert t.regs.get(REG_TX_GAIN_CCK) == 0x14


def test_probe_replaces_invalid_eeprom_mac_with_random_local_unicast(monkeypatch):
    """dev.c:1506-1510: an all-0xFF (multicast) EEPROM MAC is replaced by eth_random_addr()."""
    pr, _, _ = _probe_with_fake_eeprom(monkeypatch, 0x00, word_value=0xFFFF)
    assert pr.mac != bytes.fromhex("ffffffffffff")
    assert pr.mac[0] & 0x01 == 0
    assert pr.mac[0] & 0x02 == 0x02


def test_is_valid_ether_addr():
    from wifit3.chips.rtl8187.probe import is_valid_ether_addr

    assert is_valid_ether_addr(bytes.fromhex("020000000001"))
    assert not is_valid_ether_addr(bytes(6))
    assert not is_valid_ether_addr(bytes.fromhex("010000000001"))


def _bulk_writer() -> tuple[list[bytes], object]:
    from unittest.mock import MagicMock

    writes: list[bytes] = []
    dev = MagicMock()
    dev.write = lambda ep, payload, timeout: (writes.append(bytes(payload)), len(payload))[1]
    return writes, dev


def test_inject_frame_sends_zlp_on_a_multiple_of_wmaxpacketsize():
    """URB_ZERO_PACKET (dev.c:315) pads against the endpoint's wMaxPacketSize: 64 on a
    full-speed link, so a 64-byte transfer (12 B tx_hdr + 52 B frame) needs a ZLP."""
    from wifit3.chips.rtl8187.tx import inject_frame

    writes, dev = _bulk_writer()
    inject_frame(dev, bytes(52), {0x02: 64}, False)
    assert [len(w) for w in writes] == [64, 0]

    writes, dev = _bulk_writer()
    inject_frame(dev, bytes(52), {0x02: 512}, False)
    assert [len(w) for w in writes] == [64]


def test_inject_frame_sets_morefrag_from_the_frame_control():
    """dev.c:251: MOREFRAG (bit 17) follows the frame's More Fragments bit."""
    import struct

    from wifit3.chips.rtl8187.tx import inject_frame

    for is_rtl8187b in (False, True):
        for fc1, expected in ((0x00, 0), (0x04, 1 << 17)):
            writes, dev = _bulk_writer()
            frame = bytes([0x08, fc1]) + bytes(30)
            inject_frame(dev, frame, {0x02: 512, 0x04: 512, 0x05: 512, 0x06: 512,
                                      0x07: 512, 0x0C: 512}, is_rtl8187b)
            assert struct.unpack_from("<I", writes[0])[0] & (1 << 17) == expected


def test_bulk_out_max_packet_size_reads_the_endpoint_descriptor(monkeypatch):
    from unittest.mock import MagicMock

    import wifit3.chips.rtl8187.tx as tx

    monkeypatch.setattr(tx.usb.util, "find_descriptor",
                        lambda intf, bEndpointAddress: MagicMock(wMaxPacketSize=64)
                        if bEndpointAddress == 0x02 else None)
    assert tx.bulk_out_max_packet_size(MagicMock()) == 64
