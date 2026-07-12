"""TX/injection: the HTC wrapper builders, the slot-cookie bitmap, and driver.inject_frame.

Frame-control classification routes data frames to the BE data endpoint (tx_frame_hdr, 12 B) and
everything else (mgmt + control) to the mgmt endpoint (tx_mgmt_hdr, 8 B); the per-frame cookie is
the TX slot (find_first_zero_bit), freed on the WMI_TXSTATUS completion. [SRC] htc_drv_txrx.c."""
import struct
from types import SimpleNamespace

from wifit3.chips.ar9271_v2 import constants as C, tx
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver

# 24-byte stubs (no IEs): ProbeReq (mgmt), Null (data), RTS (control).
PROBE = bytes.fromhex("4000" "0000" "ffffffffffff" "001122334455" "ffffffffffff" "0000")
NULL = bytes.fromhex("4801" "0000" "001122334455" "00aabbccddee" "001122334455" "0000")
RTS = bytes.fromhex("b400" "4e04" "001122334455" "00aabbccddee")


def test_is_data_frame_classification():
    assert tx.is_data_frame(NULL) is True             # FTYPE_DATA
    assert tx.is_data_frame(PROBE) is False            # FTYPE_MGMT
    assert tx.is_data_frame(RTS) is False              # FTYPE_CTL


def test_build_mgmt_tx_wire_bytes():
    out = tx.build_mgmt_tx(5, PROBE, cookie=7)
    htc_payload_len = tx.TX_MGMT_HDR_LEN + len(PROBE)              # 8 + 24 = 32
    expected = (struct.pack("<HH", tx.HTC_FRAME_HDR_LEN + htc_payload_len, tx.HIF_TX_STREAM_TAG)
                + struct.pack(">BBH4x", 5, 0, htc_payload_len)
                + bytes([0, 0, 0, 0, tx.ATH9K_KEY_TYPE_CLEAR, tx.ATH9K_TXKEYIX_INVALID, 7, 0])
                + PROBE)
    assert out == expected
    assert out[18] == 7                                            # cookie at mgmt offset


def test_build_data_tx_wire_bytes():
    out = tx.build_data_tx(6, NULL, cookie=2)
    htc_payload_len = tx.TX_FRAME_HDR_LEN + len(NULL)             # 12 + 24 = 36
    expected = (struct.pack("<HH", tx.HTC_FRAME_HDR_LEN + htc_payload_len, tx.HIF_TX_STREAM_TAG)
                + struct.pack(">BBH4x", 6, 0, htc_payload_len)
                + struct.pack(">BBBBIBBBB", tx.ATH9K_HTC_NORMAL, 0, 0, 0, 0,
                              tx.ATH9K_KEY_TYPE_CLEAR, tx.ATH9K_TXKEYIX_INVALID, 2, 0)
                + NULL)
    assert out == expected
    assert out[22] == 2                                            # cookie at data offset


def test_tx_slots_find_first_zero_and_clear():
    slots = tx.TxSlots()
    assert [slots.get() for _ in range(4)] == [0, 1, 2, 3]
    slots.clear(1)
    assert slots.get() == 1                                        # lowest free reused
    assert slots.get() == 4                                        # then the next fresh slot


def test_txstatus_cookies_decode():
    # wmi_event_txstatus: u8 cnt, then cnt x (cookie, ts_rate, ts_flags).
    body = bytes([3, 5, 0x60, 0x01, 9, 0x50, 0x01, 2, 0x60, 0x00])
    assert tx.txstatus_cookies(body) == [5, 9, 2]
    assert tx.txstatus_cookies(b"") == []


def _replay_driver(sent):
    transport = SimpleNamespace(wlan_out=lambda data: sent.append(bytes(data)) or len(data))
    wmi = SimpleNamespace(t=transport)
    endpoints = {C.WMI_MGMT_SVC: 5, C.WMI_DATA_BE_SVC: 6}
    return AR9271V2Driver.for_replay(wmi, None, endpoints)


def test_inject_frame_routes_and_allocates_cookies():
    # _emit_frame is the sync core (the public inject_frame is the async UI wrapper around it).
    sent: list[bytes] = []
    drv = _replay_driver(sent)

    assert drv._emit_frame(PROBE) == 0                             # mgmt, slot 0
    assert drv._emit_frame(NULL) == 1                              # data, slot 1
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)
    assert sent[1] == tx.build_data_tx(6, NULL, 1)

    # A completion for cookie 0 frees the slot; the next inject reuses it.
    drv.tx_status_event(bytes([1, 0, 0x60, 0x01]))
    assert drv._emit_frame(RTS) == 0                               # control -> mgmt, slot 0 reused
    assert sent[2] == tx.build_mgmt_tx(5, RTS, 0)


async def test_inject_frame_async_returns_bool():
    # The WlanDriver contract the UI awaits: async, returns True, and actually sends the frame
    # (regression guard for the "object int can't be used in 'await' expression" crash).
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    assert await drv.inject_frame(PROBE) is True
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)


async def test_inject_frame_recycles_slots_past_bitmap():
    # Fire-and-forget inject must free its TX slot at emit time — nothing consumes WMI_TXSTATUS at
    # runtime to free it. Regression for the 256-slot leak that jammed high-rate WEP replay/chopchop
    # on AR9271 with ENOBUFS (low-volume deauth/PMKID/WPS stayed under the cap and hid it).
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    n = tx.MAX_TX_BUF_NUM + 10                                     # well past the 256-slot bitmap
    for _ in range(n):
        assert await drv.inject_frame(PROBE) is True               # would raise ENOBUFS at 257
    assert len(sent) == n
