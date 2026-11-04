"""rtl8821au_dkms TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware (synthetic frames).

The monitor entry accept-alls RXFLTMAP1, so ``_enable_rx_acks`` is a documented no-op; the tap
lives in ``_dispatch`` (raw frames), before the parser drops the ACK control frame. The tally and
arming live on the ``Driver`` base (``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8821au_dkms import tx, watchdog
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one NORMAL_RX packet: a 14-B on-wire ACK to ``ra`` (10-B MPDU
    + 4-B HW FCS, which iter_frames strips). 24-B RX desc: rxdw0 pkt_len=14, no drvinfo/shift."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 26-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl, reason."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00" + b"\x07\x00")


def _driver() -> Rtl8821auDkmsDriver:
    d = Rtl8821auDkmsDriver(MagicMock())
    d._channel = 1                        # connect() leaves the card tuned
    d._dm = watchdog.DmState(cur_ig_value=0x20, nhm_igi=0x20, rrsr_val_init=0x15F, h2l_lb=0, l2h_lb=0,
                             eeprom_thermal=0xFF, default_ofdm_index=24, bb_swing_diff_2g=0, bb_swing_diff_5g=0)
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


async def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []                      # an ACK is never handed to the frame parser


async def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    await d.enable_rx_acks()
    d._dispatch(_ack_buf(ra))                   # armed, but ra is not one of ours
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._dispatch(_ack_buf(ra))                   # never enabled -> _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = _mgmt_frame()
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_mgnt_retry_limit():
    d = _driver()
    sent: list[bytes] = []
    d.transport.bulk_out = lambda pkt: sent.append(pkt)
    frame = _mgmt_frame()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    pkt = sent[0]
    dw4 = int.from_bytes(pkt[0x10:0x14], "little")
    assert (dw4 >> 17) & 1 == 1                  # RETRY_LIMIT_ENABLE
    assert (dw4 >> 18) & 0x3F == tx.MGNT_DATA_RETRY_LIMIT
    assert pkt[tx.TXDESC_SIZE:] == frame         # HW-stamp: payload byte-for-byte unchanged
    assert pkt[16] & 0x7F == tx.DESC_RATE1M      # TX_RATE: 1M CCK on 2.4 GHz


async def test_inject_uses_6m_ofdm_on_5ghz():
    d = _driver()
    d._channel = 149
    sent: list[bytes] = []
    d.transport.bulk_out = lambda pkt: sent.append(pkt)
    assert await d.inject_frame(_mgmt_frame()) is True
    assert sent[0][16] & 0x7F == tx.DESC_RATE6M
    assert (int.from_bytes(sent[0][4:8], "little") >> 16) & 0x1F == tx.RATEID_IDX_G


async def test_inject_data_frame_uses_vht_1ss_raid_at_1m():
    d = _driver()
    d._channel = 149
    sent: list[bytes] = []
    d.transport.bulk_out = lambda pkt: sent.append(pkt)
    data_frame = b"\x08" + _mgmt_frame()[1:]   # FC type data
    assert await d.inject_frame(data_frame) is True
    assert sent[0][16] & 0x7F == tx.DESC_RATE1M
    assert (int.from_bytes(sent[0][4:8], "little") >> 16) & 0x1F == tx.RATEID_IDX_VHT_1SS
