"""rtl8192cu RX descriptor decode, PHY-status signal, TX descriptor build, bulk-OUT/IN paths and card disable.

Descriptor fixtures are packed here from the rtl8192cu/trx.h and mac.h bit layouts, independently of
the port's own getters and setters."""
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8192cu import SUPPORTED_IDS
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.core import ieee80211_select_queue_80211, rtl_op_stop, rtl_op_tx
from wifit3.chips.rtl8192cu.driver import RTL8192CUDriver
from wifit3.chips.rtl8192cu.hw import rtl92cu_card_disable
from wifit3.chips.rtl8192cu.priv import Ieee80211TxInfo, RtlPriv, RtlTcbDesc
from wifit3.chips.rtl8192cu.transport import RTL8192CUTransport
from wifit3.chips.rtl8192cu.trx import rtl92cu_tx_fill_desc
from wifit3.chips.rtl8192cu.usb import _rtl_rx_completed, _rtl_rx_work, rtl_usb_stop, rtl_usb_tx
from wifit3.chips.rtl8192cu.wifi import is_hal_stop, set_hal_start

from .fake import FakeRegisters

AP = bytes.fromhex("020000000001")
STA = bytes.fromhex("020000000002")
BROADCAST = b"\xff" * 6
FCS = b"\xde\xad\xbe\xef"


def _beacon(addr1: bytes = BROADCAST) -> bytes:
    return b"\x80\x00\x00\x00" + addr1 + AP + AP + b"\x10\x00" + bytes(12) + b"\x00\x04test"


def _rx_desc(pkt_len: int, *, drvinfo_units: int = 0, shift: int = 0, phystatus: bool = False, crc: bool = False,
             icv: bool = False, swdec: bool = True, rxmcs: int = C.DESC_RATE1M, ht: bool = False, bw40: bool = False,
             paggr: bool = False, faggr: bool = False, tsfl: int = 0) -> bytes:
    """rx_desc_92c (mac.h:68): dw0 length[13:0] crc32[14] icverror[15] drv_infosize[19:16] shift[25:24]
    phystatus[26] swdec[27]; dw1 paggr[14] faggr[15]; dw3 rxmcs[5:0] rxht[6] splcp[8] bw[9]; dw5 tsfl."""
    dw0 = (pkt_len | crc << 14 | icv << 15 | drvinfo_units << 16 | shift << 24 | phystatus << 26 | swdec << 27)
    dw1 = paggr << 14 | faggr << 15
    dw3 = rxmcs | ht << 6 | bw40 << 9
    return struct.pack("<6I", dw0, dw1, 0, dw3, 0, tsfl)


def _ofdm_phy_status(gain_a: int, gain_b: int, pwdb_all: int, rxevm0: int = -20) -> bytes:
    """rx_fwinfo_92c (mac.h:50): gain_trsw[4], pwdb_all, cfosho[4], cfotail[4], rxevm[2], rxsnr[4], ..."""
    info = bytearray(32)
    info[0], info[1], info[4] = gain_a, gain_b, pwdb_all
    info[13] = rxevm0 & 0xFF
    info[15], info[16] = (-7) & 0xFF, 9
    return bytes(info)


def _cck_phy_status(cck_agc_rpt: int, sq_rpt: int = 30) -> bytes:
    """phy_sts_cck_8192s_t (rtl8192ce/def.h:101): adc_pwdb_X[4], sq_rpt, cck_agc_rpt."""
    info = bytearray(32)
    info[4], info[5] = sq_rpt, cck_agc_rpt
    return bytes(info)


def _started_priv() -> tuple[RtlPriv, FakeRegisters]:
    io = FakeRegisters()
    priv = RtlPriv(io)
    priv.usb.state = C.USB_STATE_START
    priv.status |= C.BIT(C.RTL_STATUS_INTERFACE_START)
    priv.usb.out_eps[:2] = [2, 3]
    priv.usb.ep_mapping = [3, 3, 2, 2, 2, 2, 2]
    priv.usb.max_bulk_out_size = C.USB_HIGH_SPEED_BULK_SIZE
    priv.conf_center_freq = 2437
    set_hal_start(priv.rtlhal)
    return priv, io


def _receive(priv: RtlPriv, transfer: bytes):
    skb = _rtl_rx_completed(priv, transfer)
    return None if skb is None else _rtl_rx_work(priv, skb)


def test_ofdm_frame_decodes_rate_signal_and_keeps_the_fcs():
    priv, _io = _started_priv()
    mpdu = _beacon() + FCS
    phy = _ofdm_phy_status(gain_a=0x30, gain_b=0x20, pwdb_all=0x80)
    transfer = _rx_desc(len(mpdu), drvinfo_units=4, phystatus=True, rxmcs=C.DESC_RATE24M, tsfl=0x12345678) + phy + mpdu
    frame, rx_status = _receive(priv, transfer)
    assert frame == mpdu
    assert rx_status.signal == (0x80 >> 1) - 110 + 10
    assert rx_status.rate_idx == 8 and rx_status.encoding == C.RX_ENC_LEGACY
    assert rx_status.mactime == 0x12345678 and rx_status.freq == 2437
    assert not rx_status.failed_fcs_crc and not rx_status.decrypted
    assert priv.stats.rx_snr_db[:2] == [-3, 4]
    assert priv.stats.rxbytesunicast == 0


@pytest.mark.parametrize("agc, high_power, cfosho0, expected", [
    (0x8a, False, 0x00, -26 - 0x0a),
    (0xca, False, 0x00, -46 - 0x0a),
    (0x0a, False, 0x00, 16 - 0x0a),
    (0x45, True, 0x20, -12 - (0x05 << 1)),
])
def test_cck_signal_follows_the_agc_report(agc, high_power, cfosho0, expected):
    priv, _io = _started_priv()
    priv.phy.cck_high_power = high_power
    mpdu = _beacon() + FCS
    phy = bytearray(_cck_phy_status(agc))
    if high_power:
        phy[5] = (agc & 0x1F) | cfosho0
    transfer = _rx_desc(len(mpdu), drvinfo_units=4, phystatus=True, rxmcs=C.DESC_RATE1M) + bytes(phy) + mpdu
    _frame, rx_status = _receive(priv, transfer)
    assert rx_status.signal == expected + 10


def test_ht40_mcs_frame_sets_ht_encoding_and_bandwidth():
    priv, _io = _started_priv()
    mpdu = _beacon() + FCS
    transfer = _rx_desc(len(mpdu), rxmcs=C.DESC_RATEMCS0 + 7, ht=True, bw40=True, swdec=False) + mpdu
    _frame, rx_status = _receive(priv, transfer)
    assert rx_status.encoding == C.RX_ENC_HT and rx_status.rate_idx == 7
    assert rx_status.bw == C.RATE_INFO_BW_40 and rx_status.decrypted
    assert rx_status.signal == 10


def test_crc_error_frames_are_dropped_and_icv_errors_are_kept():
    priv, _io = _started_priv()
    mpdu = _beacon(STA) + FCS
    assert _receive(priv, _rx_desc(len(mpdu), crc=True) + mpdu) is None
    frame, _status = _receive(priv, _rx_desc(len(mpdu), icv=True) + mpdu)
    assert frame == mpdu
    assert priv.stats.rxbytesunicast == len(mpdu)


def test_descriptor_shift_skips_the_pad_before_the_frame():
    priv, _io = _started_priv()
    mpdu = _beacon() + FCS
    phy = _ofdm_phy_status(0x30, 0x30, 0x90)
    transfer = _rx_desc(len(mpdu), drvinfo_units=4, shift=2, phystatus=True, rxmcs=C.DESC_RATE6M) + b"\x00\x00" \
        + phy + mpdu
    frame, rx_status = _receive(priv, transfer)
    assert frame == mpdu
    assert rx_status.signal == (0x90 >> 1) - 110 + 10


def test_short_transfers_and_a_stopped_adapter_drop_the_buffer():
    priv, _io = _started_priv()
    assert _receive(priv, _rx_desc(20) + bytes(20)) is None
    mpdu = _beacon() + FCS
    priv.usb.state = C.USB_STATE_STOP
    assert _receive(priv, _rx_desc(len(mpdu)) + mpdu) is None


def test_addba_request_is_dropped_only_while_scanning():
    priv, _io = _started_priv()
    action = b"\xd0\x00\x00\x00" + STA + AP + AP + b"\x00\x00" + bytes([C.ACT_CAT_BA, C.ACT_ADDBAREQ, 1]) + FCS
    transfer = _rx_desc(len(action)) + action
    assert _receive(priv, transfer) is not None
    priv.mac80211.act_scanning = True
    assert _receive(priv, transfer) is None


def test_bssid_match_feeds_the_signal_statistics():
    priv, _io = _started_priv()
    priv.phy.num_total_rfpath = 1
    priv.dm.undec_sm_pwdb = -1
    priv.mac80211.bssid = AP
    mpdu = _beacon() + FCS
    phy = _ofdm_phy_status(0x30, 0x30, 0x90)
    _receive(priv, _rx_desc(len(mpdu), drvinfo_units=4, phystatus=True, rxmcs=C.DESC_RATE54M) + phy + mpdu)
    assert priv.dm.undec_sm_pwdb == 100 + (0x90 >> 1) - 110
    assert priv.stats.ui_rssi.total_num == 1
    assert priv.stats.signal_strength < 0
    assert priv.stats.rx_rssi_percentage[0] == 86


def _tx_fields(desc: bytes) -> dict[str, int]:
    """trx.h:151-371, read back by hand."""
    dw = struct.unpack("<8I", desc[:32])
    return {
        "pkt_size": dw[0] & 0xFFFF, "offset": (dw[0] >> 16) & 0xFF, "bmc": (dw[0] >> 24) & 1,
        "last_seg": (dw[0] >> 26) & 1, "first_seg": (dw[0] >> 27) & 1, "own": (dw[0] >> 31) & 1,
        "macid": dw[1] & 0x1F, "agg_break": (dw[1] >> 6) & 1, "queue_sel": (dw[1] >> 8) & 0x1F,
        "rate_id": (dw[1] >> 16) & 0xF, "pkt_offset": (dw[1] >> 26) & 0x1F,
        "more_frag": (dw[2] >> 17) & 1, "seq": (dw[3] >> 16) & 0xFFF, "pkt_id": dw[3] >> 28,
        "hwseq_en": (dw[4] >> 7) & 1, "use_rate": (dw[4] >> 8) & 1, "rts_enable": (dw[4] >> 12) & 1,
        "hw_rts_enable": (dw[4] >> 13) & 1, "cts2self": (dw[4] >> 11) & 1, "rts_rate": dw[4] & 0x1F,
        "data_bw": (dw[4] >> 25) & 1, "data_sc": (dw[4] >> 20) & 3,
        "tx_rate": dw[5] & 0x3F, "data_shortgi": (dw[5] >> 6) & 1,
        "data_rate_fb_limit": (dw[5] >> 8) & 0x1F, "rts_rate_fb_limit": (dw[5] >> 13) & 0xF,
        "checksum": dw[7] & 0xFFFF,
    }


def _xor16(desc: bytes) -> int:
    value = 0
    for word in struct.unpack("<16H", desc[:32]):
        value ^= word
    return value


def _deauth(addr1: bytes, seq: int) -> bytes:
    return b"\xc0\x00\x00\x00" + addr1 + AP + AP + (seq << 4).to_bytes(2, "little") + b"\x07\x00"


@pytest.mark.parametrize("useramask", [False, True])
def test_tx_descriptor_round_trip_for_a_broadcast_deauth(useramask):
    priv, _io = _started_priv()
    priv.dm.useramask = useramask
    frame = _deauth(BROADCAST, 0x123)
    info = Ieee80211TxInfo(queue_mapping=ieee80211_select_queue_80211(frame))
    skb = rtl92cu_tx_fill_desc(priv, frame, info, RtlTcbDesc())
    assert skb[C.RTL_TX_HEADER_SIZE:] == frame
    f = _tx_fields(skb)
    assert (f["pkt_size"], f["offset"], f["pkt_offset"]) == (len(frame), 32, 0)
    assert (f["own"], f["first_seg"], f["last_seg"], f["bmc"]) == (1, 1, 1, 1)
    assert f["queue_sel"] == C.QSLT_MGNT and f["seq"] == 0x123 and f["agg_break"] == 1
    assert (f["tx_rate"], f["use_rate"], f["data_shortgi"]) == (C.DESC_RATE1M, 1, 0)
    assert (f["data_rate_fb_limit"], f["rts_rate_fb_limit"]) == (0x1F, 0xF)
    assert (f["rts_enable"], f["hw_rts_enable"], f["cts2self"], f["hwseq_en"], f["pkt_id"]) == (0, 0, 0, 0, 0)
    if useramask:
        assert (f["rate_id"], f["macid"]) == (C.RATR_INX_WIRELESS_MC, 0)
    else:
        assert (f["rate_id"], f["macid"]) == ((0xC + C.RATR_INX_WIRELESS_MC) & 0xF, C.RATR_INX_WIRELESS_MC)
    assert _xor16(skb) == 0
    assert f["checksum"] == _xor16(skb[:28] + bytes(4))


def test_unicast_tods_data_uses_addr3_as_da_and_the_be_queue():
    priv, _io = _started_priv()
    null_data = b"\x48\x01\x00\x00" + AP + STA + BROADCAST + b"\x50\x00"
    info = Ieee80211TxInfo(queue_mapping=ieee80211_select_queue_80211(null_data))
    assert info.queue_mapping == C.IEEE80211_AC_BE
    ep_num, skb = rtl_usb_tx(priv, null_data, info)
    f = _tx_fields(skb)
    assert f["bmc"] == 1                                   # ieee80211_get_DA: ToDS -> addr3
    assert f["queue_sel"] == C.QSLT_BE and ep_num == priv.usb.ep_mapping[C.RTL_TXQ_BE]
    assert f["use_rate"] == 1 and f["tx_rate"] == C.DESC_RATE1M


QOS_DATA_TID5 = b"\x88\x01\x00\x00" + AP + STA + AP + b"\x00\x00" + b"\x05\x00"
QOS_DATA_A4_TID1 = b"\x88\x03\x00\x00" + AP + STA + AP + b"\x00\x00" + STA + b"\x01\x00"


@pytest.mark.parametrize("frame, ac", [
    (_deauth(STA, 0), C.IEEE80211_AC_VO),
    (QOS_DATA_TID5, C.IEEE80211_AC_VI),
    (QOS_DATA_A4_TID1, C.IEEE80211_AC_BK),
])
def test_injected_frames_queue_like_mac80211(frame, ac):
    assert ieee80211_select_queue_80211(frame) == ac


def test_rts_cts_and_40mhz_dup_flags_reach_the_descriptor():
    priv, _io = _started_priv()
    priv.mac80211.bw_40 = True
    data = b"\x08\x00\x00\x00" + AP + STA + AP + b"\x00\x00" + bytes(8)
    info = Ieee80211TxInfo(rate_idx=11, rate_flags=C.IEEE80211_TX_RC_USE_RTS_CTS | C.IEEE80211_TX_RC_DUP_DATA)
    f = _tx_fields(rtl92cu_tx_fill_desc(priv, data, info, RtlTcbDesc()))
    assert (f["rts_enable"], f["hw_rts_enable"], f["rts_rate"]) == (1, 1, C.DESC_RATE24M)
    assert (f["data_bw"], f["data_sc"]) == (1, 3)
    assert f["tx_rate"] == C.DESC_RATE54M and f["use_rate"] == 0


def test_tx_is_dropped_when_the_hal_is_stopped():
    priv, _io = _started_priv()
    priv.rtlhal.state = C._HAL_STATE_STOP
    assert rtl_op_tx(priv, _deauth(STA, 1), Ieee80211TxInfo(), rtl_usb_tx) is None


def test_bulk_write_adds_a_zero_length_packet_on_a_max_packet_multiple():
    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)
    transport = RTL8192CUTransport(dev)
    transport.bulk_write(2, bytes(512), 512, 1000)
    transport.bulk_write(2, bytes(100), 512, 1000)
    assert [len(call.args[1]) for call in dev.write.call_args_list] == [512, 0, 100]


def test_card_disable_powers_down_with_the_hw_state_machine():
    priv, io = _started_priv()
    priv.usb.disablehwsm = True
    priv.rtlhal.fw_version = 88
    io.regs[C.REG_MCUFWDL] = C.BIT(1)
    rtl92cu_card_disable(priv)
    writes = [(a, v) for k, a, _w, v in io.log if k == "W"]
    assert writes.index((C.MSR, C.MSR_NOLINK)) < writes.index((C.REG_TXPAUSE, 0xFF))
    assert (C.REG_TXPAUSE, 0xFF) in writes
    assert (C.REG_APSD_CTRL, C.APSDOFF) in writes
    assert io.writes(C.REG_SYS_FUNC_EN)[-2:] == [0x16, 0x14]
    assert io.writes(C.REG_HMETFR + 3) == [0x20]
    assert io.writes(C.REG_SYS_FUNC_EN + 1) == [0x54]
    assert io.writes(C.REG_LEDCFG0) == [0x8080]
    assert io.writes(C.REG_APS_FSMCO) == [C.APDM_HOST | C.AFSM_HSUS | C.PFM_ALDN]
    assert writes[-1] == (C.REG_RSV_CTRL, 0x0E)
    assert C.REG_LDOA15_CTRL not in io.addrs("W")
    assert priv.psc.cur_ps_level & C.RT_RF_OFF_LEVL_HALT_NIC and not priv.phy.iqk_initialized


def test_card_disable_without_hwsm_isolates_and_cuts_ldo_power():
    priv, io = _started_priv()
    priv.usb.disablehwsm = False
    priv.rtlhal.fw_version = 0x20
    io.regs[C.REG_LDOV12D_CTRL] = 0x55
    rtl92cu_card_disable(priv)
    assert io.writes(C.REG_SYS_ISO_CTRL)[-1] == 0xF9           # RF writes hit 0x0000: no BB register defs here
    assert io.writes(C.REG_SYS_ISO_CTRL + 1) == [0x82]
    assert io.writes(C.REG_LDOA15_CTRL) == [0x04]
    assert io.writes(C.REG_LDOV12D_CTRL) == [0x54]
    assert io.writes(C.REG_MCUFWDL) == [0]


def test_op_stop_stops_the_usb_side_before_powering_off():
    priv, io = _started_priv()
    priv.mac80211.bssid = AP
    order = []
    rtl_op_stop(priv, lambda: rtl_usb_stop(priv, lambda: order.append(("kill", len(io.log)))))
    assert order == [("kill", 0)]
    assert is_hal_stop(priv.rtlhal) and priv.usb.state == C.USB_STATE_STOP
    assert priv.mac80211.bssid == bytes(6)
    assert io.writes(C.REG_RSV_CTRL) == [0x0E]
    io.log.clear()
    rtl_op_stop(priv, lambda: pytest.fail("adapter_stop on a stopped HAL"))
    assert io.log == []


async def test_driver_dispatch_strips_the_fcs_and_tallies_acks():
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    driver.priv, _io = _started_priv()
    packets = []
    driver.register_rx_callback(packets.append)
    mpdu = _beacon() + FCS
    phy = _ofdm_phy_status(0x30, 0x30, 0x90)
    driver._dispatch(_rx_desc(len(mpdu), drvinfo_units=4, phystatus=True, rxmcs=C.DESC_RATE6M) + phy + mpdu)
    assert len(packets) == 1
    assert packets[0].rssi == (0x90 >> 1) - 110 + 10
    await driver.enable_rx_acks()
    driver._our_tx_macs.add(STA)
    ack = b"\xd4\x00\x00\x00" + STA + FCS
    driver._dispatch(_rx_desc(len(ack), drvinfo_units=4, phystatus=True, rxmcs=C.DESC_RATE24M) + phy + ack)
    assert driver.acks_seen(STA) == 1


async def test_close_powers_off_and_releases():
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    driver.priv, io = _started_priv()
    driver.transport = SimpleNamespace(release=MagicMock())
    driver._started = True
    await driver.close()
    assert io.writes(C.REG_APS_FSMCO) == [C.APDM_HOST | C.AFSM_HSUS | C.PFM_ALDN]
    driver.transport.release.assert_called_once()
    assert not driver._started
