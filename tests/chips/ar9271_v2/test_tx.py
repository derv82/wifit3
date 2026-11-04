"""TX/injection: the HTC wrapper builders, the slot-cookie bitmap, and driver.inject_frame.

Frame-control classification routes data frames to the BE data endpoint (tx_frame_hdr, 12 B) and
everything else (mgmt + control) to the mgmt endpoint (tx_mgmt_hdr, 8 B); the per-frame cookie is
the TX slot (find_first_zero_bit), freed by its WMI_TXSTATUS, the 3 s cleanup timeout, or a
channel change's drain. [SRC] htc_drv_txrx.c."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import struct
import threading
import time
from types import SimpleNamespace

from wifit3.chips.ar9271_v2 import bringup, chan, constants as C, tx
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


ENDPOINTS = {C.WMI_MGMT_SVC: 5, C.WMI_CAB_SVC: 3, C.WMI_DATA_BE_SVC: 6, C.WMI_DATA_BK_SVC: 7,
             C.WMI_DATA_VI_SVC: 8, C.WMI_DATA_VO_SVC: 9}


def txstatus(*entries: tuple[int, int]) -> bytes:
    """wmi_event_txstatus: u8 cnt, then cnt x (cookie, ts_rate with the epid in bits 4-7, ts_flags)."""
    body = bytes([len(entries)])
    for cookie, epid in entries:
        body += bytes([cookie, epid << 4, 0x01])
    return body


def test_tx_slots_find_first_zero_and_clear():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    assert [state.tx_get_slot() for _ in range(4)] == [0, 1, 2, 3]
    state.tx_clear_slot(1)
    assert state.tx_get_slot() == 1                                # lowest free reused
    assert state.tx_get_slot() == 4                                # then the next fresh slot


def test_tx_slots_allocate_unique_cookies_concurrently():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    with ThreadPoolExecutor(max_workers=32) as executor:
        cookies = list(executor.map(lambda _: state.tx_get_slot(), range(128)))
    assert sorted(cookies) == list(range(128))


def test_no_free_slot_is_enobufs():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    for _ in range(tx.MAX_TX_BUF_NUM):
        state.tx_get_slot()
    assert state.tx_get_slot() == -1


def test_txstatus_frees_the_queued_frame_on_its_endpoint():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    for epid in (5, 6, 5):
        state.txep(epid, state.tx_get_slot(), 0.0)
    state.txstatus(txstatus((0, 5), (1, 6)))
    assert state.tx_get_slot() == 0
    assert state.tx_get_slot() == 1
    assert state.tx_get_slot() == 3                                # cookie 2 still awaits its status


def test_txstatus_for_another_endpoint_waits_for_the_cleanup_timer():
    # ath9k_htc_tx_get_packet matches cookie AND endpoint; a miss is kept, retried every tick.
    state = tx.Ath9kHtcTx(ENDPOINTS)
    state.txep(5, state.tx_get_slot(), 0.0)
    state.txstatus(txstatus((0, 6)))
    assert state.tx_get_slot() == 1
    assert len(state.pending_tx_events) == 1
    for _ in range(tx.ATH9K_HTC_TX_TIMEOUT_COUNT):
        state.cleanup_timer(0.0)
    assert state.pending_tx_events == []


def test_cleanup_timer_frees_frames_sent_over_3s_ago():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    state.txep(5, state.tx_get_slot(), 10.0)
    state.cleanup_timer(10.0 + tx.ATH9K_HTC_TX_TIMEOUT_INTERVAL)
    assert state.tx_get_slot() == 1
    state.cleanup_timer(10.0 + tx.ATH9K_HTC_TX_TIMEOUT_INTERVAL + 0.001)
    assert state.tx_get_slot() == 0


def test_tx_drain_frees_every_queued_frame():
    state = tx.Ath9kHtcTx(ENDPOINTS)
    for epid in (5, 6, 7):
        state.txep(epid, state.tx_get_slot(), 0.0)
    state.tx_drain()
    assert state.tx_get_slot() == 0


def _replay_driver(sent):
    transport = SimpleNamespace(wlan_out=lambda data: sent.append(bytes(data)) or len(data))
    wmi = SimpleNamespace(t=transport)
    endpoints = {C.WMI_MGMT_SVC: 5, C.WMI_DATA_BE_SVC: 6}
    return AR9271V2Driver.for_replay(wmi, None, endpoints)


def test_inject_frame_routes_and_allocates_cookies():
    # _emit_frame is the sync core (the public inject_frame is the async UI wrapper around it).
    sent: list[bytes] = []
    drv = _replay_driver(sent)

    assert drv._emit_frame(PROBE, 0.0) == 0                        # mgmt, slot 0
    assert drv._emit_frame(NULL, 0.0) == 1                         # data, slot 1
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)
    assert sent[1] == tx.build_data_tx(6, NULL, 1)

    # A completion for cookie 0 on the mgmt endpoint frees the slot; the next inject reuses it.
    drv._wmi_event_tasklet(tx.WMI_TXSTATUS_EVENTID, txstatus((0, 5)))
    assert drv._emit_frame(RTS, 0.0) == 0                          # control -> mgmt, slot 0 reused
    assert sent[2] == tx.build_mgmt_tx(5, RTS, 0)


def test_failed_usb_submission_releases_cookie():
    attempts = 0
    sent: list[bytes] = []

    def wlan_out(data):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("USB submission failed")
        sent.append(bytes(data))
        return len(data)

    transport = SimpleNamespace(wlan_out=wlan_out)
    wmi = SimpleNamespace(t=transport)
    endpoints = {C.WMI_MGMT_SVC: 5, C.WMI_DATA_BE_SVC: 6}
    drv = AR9271V2Driver.for_replay(wmi, None, endpoints)

    try:
        drv._emit_frame(PROBE, 0.0)
    except OSError:
        pass
    else:
        raise AssertionError("failed USB submission did not propagate")

    assert drv._emit_frame(PROBE, 0.0) == 0
    assert sent == [tx.build_mgmt_tx(5, PROBE, 0)]


async def test_inject_frame_async_returns_bool():
    # The Driver contract the UI awaits: async, returns True, and actually sends the frame
    # (regression guard for the "object int can't be used in 'await' expression" crash).
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    assert await drv.inject_frame(PROBE) is True
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)


def test_fast_channel_change_falls_back_when_chip_is_not_alive(mocker):
    wmi = SimpleNamespace(cmd=mocker.Mock())
    hw = SimpleNamespace(getnf=mocker.Mock(), check_alive=mocker.Mock(return_value=False))
    channel = chan.channel_2ghz(6)
    full = mocker.patch.object(bringup, "_full_channel_change_stopped")
    restart = mocker.patch.object(bringup, "_restart_after_channel_change")

    bringup.fast_channel_change(wmi, hw, channel)

    full.assert_called_once_with(wmi, hw, channel)
    restart.assert_called_once_with(wmi, hw, channel)


async def test_scan_tune_uses_fast_channel_change(mocker):
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    drv.hw = SimpleNamespace(curchan=chan.channel_2ghz(1))
    fast = mocker.patch.object(bringup, "fast_channel_change")
    full = mocker.patch.object(bringup, "full_channel_change")

    assert await drv.set_channel(6, scan=True) is True

    fast.assert_called_once()
    full.assert_not_called()


async def test_settled_tune_uses_full_channel_change(mocker):
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    drv.hw = SimpleNamespace(curchan=chan.channel_2ghz(1))
    fast = mocker.patch.object(bringup, "fast_channel_change")
    full = mocker.patch.object(bringup, "full_channel_change")

    assert await drv.set_channel(6, scan=False) is True

    full.assert_called_once()
    fast.assert_not_called()


def test_channel_change_and_tx_share_operation_lock(mocker):
    class OverlapTracker:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def operation(self, *args):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1

    tracker = OverlapTracker()
    transport = SimpleNamespace(wlan_out=lambda data: tracker.operation(data) or len(data))
    wmi = SimpleNamespace(t=transport)
    endpoints = {C.WMI_MGMT_SVC: 5, C.WMI_DATA_BE_SVC: 6}
    drv = AR9271V2Driver.for_replay(wmi, object(), endpoints)
    mocker.patch.object(bringup, "full_channel_change", side_effect=tracker.operation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tune = executor.submit(drv._do_channel_change, chan.channel_2ghz(1), False)
        emit = executor.submit(drv._emit_frame, PROBE, 0.0)
        tune.result()
        emit.result()

    assert tracker.max_active == 1


async def test_concurrent_injects_serialize_usb_submissions():
    class OverlapDetectingTransport:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def wlan_out(self, data):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.005)
            with self.lock:
                self.active -= 1
            return len(data)

    transport = OverlapDetectingTransport()
    wmi = SimpleNamespace(t=transport)
    endpoints = {C.WMI_MGMT_SVC: 5, C.WMI_DATA_BE_SVC: 6}
    drv = AR9271V2Driver.for_replay(wmi, None, endpoints)

    assert all(await asyncio.gather(*(drv.inject_frame(PROBE) for _ in range(32))))
    assert transport.max_active == 1


async def test_inject_frame_drops_the_frame_while_every_slot_awaits_its_txstatus():
    # ath9k_htc_tx: no free TX slot -> the frame is dropped; its WMI_TXSTATUS frees the slot.
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    for _ in range(tx.MAX_TX_BUF_NUM):
        assert await drv.inject_frame(PROBE) is True
    assert await drv.inject_frame(PROBE) is False
    drv._wmi_event_tasklet(tx.WMI_TXSTATUS_EVENTID, txstatus((7, 5)))
    assert await drv.inject_frame(PROBE) is True
    assert sent[-1] == tx.build_mgmt_tx(5, PROBE, 7)
    assert len(sent) == tx.MAX_TX_BUF_NUM + 1
