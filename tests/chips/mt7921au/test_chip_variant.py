"""Runtime NIC-capability discriminators for mt7921au (cross-card generalization).

mt7921/MT7961 is firmware-driven: per-card EFUSE cal (TX power, PA, temp) is applied
inside the MCU and never surfaces host-side, so the only genuinely per-card VALUE is the
MAC (already read at runtime). The host-side branches that are gated on the runtime
GET_NIC_CAPAB reply — a 1:1 mirror of the kernel — are:

  - antenna_mask = BIT(nss) - 1            [SRC mt7921/mcu.c:550-551]
      * SET_RX_PATH tx_streams/rx_streams  (mcu.set_chan_info)
      * RX RSSI chain loop                 [SRC mt7921/mac.c:365-378] (rx.decode_frame)
  - has_2ghz/has_5ghz (hw_path bits) + has_6ghz (MT_NIC_CAP_6G): the TX-power band sweep
      gate + last_ch pick  [SRC mt76_connac_mcu.c:2183-2188,2260-2284] (txpower)

Both captured reference units (pau0f + AXML) report the SAME caps — nss=2 (antenna_mask
0x3), hw_path=0xF (2.4+5 GHz), has_6ghz=1 — so every gate defaults to that config and the
cold-boot wire stays byte-identical (scripts/chips/mt7921au/verify_pcap.py PASS).
"""
import struct

from wifit3.chips.mt7921au import rx, txpower
from wifit3.chips.mt7921au.constants import MT_RXD1_NORMAL_GROUP_3, MT_RXD1_NORMAL_GROUP_5
from wifit3.chips.mt7921au.mcu import (
    EXT_CMD_CHANNEL_SWITCH,
    EXT_CMD_SET_RX_PATH,
    MT_NIC_CAP_6G,
    MT_NIC_CAP_MAC_ADDR,
    MT_NIC_CAP_PHY,
    DEFAULT_CHANDEF,
    NicCaps,
    parse_nic_capability,
    set_chan_info,
)


# ---------------------------------------------------------------------------
# GET_NIC_CAPAB reply builders (mt7921_mcu_get_nic_capability wire shape).
# resp = [36B rxd/cap_hdr preamble] + n_element(le16) + rsv(2) + TLVs, each
# {type:le32, len:le32, data[len]}. parse reads body from byte 36.
# ---------------------------------------------------------------------------
def _phy_cap(nss: int, hw_path: int) -> bytes:
    """struct mt7921_phy_cap (12 B): nss @4, hw_path @10."""
    b = bytearray(12)
    b[4] = nss
    b[10] = hw_path
    return bytes(b)


def _resp(tlvs: list[tuple[int, bytes]]) -> bytes:
    body = struct.pack("<HH", len(tlvs), 0)
    for t, d in tlvs:
        body += struct.pack("<II", t, len(d)) + d
    return b"\x00" * 36 + body


_REF_MAC = bytes.fromhex("9cefd5f644a4")


def _reference_resp() -> bytes:
    """The captured reference reply: MAC + PHY(nss=2, hw_path=0xF) + 6G=1."""
    return _resp([
        (MT_NIC_CAP_MAC_ADDR, _REF_MAC),
        (MT_NIC_CAP_PHY, _phy_cap(2, 0xF)),
        (MT_NIC_CAP_6G, b"\x01"),
    ])


# ---------------------------------------------------------------------------
# parse_nic_capability — the runtime discriminator source.
# ---------------------------------------------------------------------------
def test_parse_reference_caps():
    caps = parse_nic_capability(_reference_resp())
    assert caps.mac == "9c:ef:d5:f6:44:a4"
    assert caps.antenna_mask == 0x3
    assert caps.has_2ghz is True and caps.has_5ghz is True and caps.has_6ghz is True
    assert caps.is_reference is True


def test_parse_1x1_sets_antenna_mask_1():
    """nss=1 -> antenna_mask = BIT(1)-1 = 0x1, no longer the reference."""
    caps = parse_nic_capability(_resp([(MT_NIC_CAP_PHY, _phy_cap(1, 0xF)),
                                       (MT_NIC_CAP_6G, b"\x01")]))
    assert caps.antenna_mask == 0x1
    assert caps.is_reference is False


def test_parse_2ghz_only_masks_5ghz():
    """hw_path bit WF0_5G clear -> has_5ghz False."""
    caps = parse_nic_capability(_resp([(MT_NIC_CAP_PHY, _phy_cap(2, 0x1)),
                                       (MT_NIC_CAP_6G, b"\x01")]))
    assert caps.has_2ghz is True and caps.has_5ghz is False
    assert caps.is_reference is False


def test_parse_missing_6g_tlv_defaults_false():
    """No MT_NIC_CAP_6G TLV -> has_6ghz False (kernel default), not the reference."""
    caps = parse_nic_capability(_resp([(MT_NIC_CAP_PHY, _phy_cap(2, 0xF))]))
    assert caps.has_6ghz is False
    assert caps.is_reference is False


def test_parse_empty_resp_gives_reference_defaults():
    """A malformed/short reply keeps the give-it-a-shot reference defaults for the
    always-present fields (antenna_mask 0x3, 2.4+5 GHz); has_6ghz stays kernel-default
    False."""
    caps = parse_nic_capability(b"")
    assert caps.antenna_mask == 0x3
    assert caps.has_2ghz is True and caps.has_5ghz is True and caps.has_6ghz is False


def test_parse_nss0_keeps_reference_mask():
    """Implausible nss=0 must not zero antenna_mask (would break RX / SET_RX_PATH)."""
    caps = parse_nic_capability(_resp([(MT_NIC_CAP_PHY, _phy_cap(0, 0xF))]))
    assert caps.antenna_mask == 0x3


# ---------------------------------------------------------------------------
# txpower.rate_txpower_payloads — band gating + last_ch. tlv is the first 44 B of
# each payload: num_ch @4, tlv_band @5 (1/2/3 = 2.4/5/6 GHz), last_msg @6.
# ---------------------------------------------------------------------------
def _bands(payloads):
    return {p[5] for p in payloads}


def _last_msg_band(payloads):
    hits = [p[5] for p in payloads if p[6] == 1]
    assert len(hits) == 1, f"exactly one last_msg batch expected, got {len(hits)}"
    return hits[0]


def test_txpower_reference_all_three_bands():
    payloads = txpower.rate_txpower_payloads()
    assert _bands(payloads) == {1, 2, 3}
    assert _last_msg_band(payloads) == 3          # last_ch = 233 (6 GHz)


def test_txpower_none_equals_reference_caps():
    """caps=None and the parsed reference caps produce byte-identical payloads —
    this is what keeps the cold-boot wire unchanged."""
    ref = NicCaps(antenna_mask=0x3, has_2ghz=True, has_5ghz=True, has_6ghz=True)
    assert txpower.rate_txpower_payloads(None) == txpower.rate_txpower_payloads(ref)


def test_txpower_no_6ghz_drops_band_and_moves_last_msg():
    caps = NicCaps(has_2ghz=True, has_5ghz=True, has_6ghz=False)
    payloads = txpower.rate_txpower_payloads(caps)
    assert _bands(payloads) == {1, 2}
    assert _last_msg_band(payloads) == 2          # last_ch = 177 (5 GHz)


def test_txpower_2ghz_only():
    caps = NicCaps(has_2ghz=True, has_5ghz=False, has_6ghz=False)
    payloads = txpower.rate_txpower_payloads(caps)
    assert _bands(payloads) == {1}
    assert _last_msg_band(payloads) == 1          # last_ch = 14 (2.4 GHz)


# ---------------------------------------------------------------------------
# set_chan_info — SET_RX_PATH streams thread antenna_mask (req[3]=tx, req[4]=rx).
# ---------------------------------------------------------------------------
def test_set_rx_path_streams_reference_2x2():
    _, req = set_chan_info(EXT_CMD_SET_RX_PATH, DEFAULT_CHANDEF, antenna_mask=0x3)
    assert req[3] == 2          # tx_streams = hweight8(0x3)
    assert req[4] == 0x3        # rx_streams = antenna_mask (raw, SET_RX_PATH path)


def test_set_rx_path_streams_1x1():
    _, req = set_chan_info(EXT_CMD_SET_RX_PATH, DEFAULT_CHANDEF, antenna_mask=0x1)
    assert req[3] == 1
    assert req[4] == 0x1


def test_channel_switch_rx_streams_is_hweight():
    """The CHANNEL_SWITCH path collapses rx_streams to hweight8 (kernel branch)."""
    _, req = set_chan_info(EXT_CMD_CHANNEL_SWITCH, DEFAULT_CHANDEF, antenna_mask=0x3)
    assert req[3] == 2 and req[4] == 2


# ---------------------------------------------------------------------------
# rx.decode_frame — RSSI iterates hweight8(antenna_mask) chains.
# ---------------------------------------------------------------------------
def _group5_rx(rcpi0: int, rcpi1: int) -> bytes:
    """Minimal connac2 RX buffer with GROUP_3+GROUP_5 so decode reaches the P-RXV
    RSSI vector (v1 @ word 14). to_rssi(rcpi) = (rcpi-220)//2, counted only when <0."""
    data = bytearray(200)
    struct.pack_into("<I", data, 0, 120)                                   # rxd0 length
    struct.pack_into("<I", data, 4, MT_RXD1_NORMAL_GROUP_3 | MT_RXD1_NORMAL_GROUP_5)
    struct.pack_into("<I", data, 8, 0)                                     # rxd2 remove_pad=0
    struct.pack_into("<I", data, 56, (rcpi0 & 0xFF) | ((rcpi1 & 0xFF) << 8))
    return bytes(data)


def test_rx_rssi_two_chains_takes_max():
    """antenna_mask 0x3 -> chains 0,1; RSSI = max(-20, -10) = -10."""
    frame = _group5_rx(rcpi0=180, rcpi1=200)      # -20, -10
    assert rx.decode_frame(frame, 0x3)[2] == -10
    assert rx.decode_frame(frame)[2] == -10       # default is the 2x2 reference


def test_rx_rssi_single_chain_ignores_chain1():
    """antenna_mask 0x1 -> chain 0 only; the better chain-1 sample is not read."""
    frame = _group5_rx(rcpi0=180, rcpi1=200)      # -20, -10
    assert rx.decode_frame(frame, 0x1)[2] == -20
