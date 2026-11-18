"""TX/injection: the HTC wrapper builders, the TX slots, and driver.inject_frame.

Frame-control classification routes data frames to the BE data endpoint (tx_frame_hdr, 12 B) and
everything else (mgmt + control) to the mgmt endpoint (tx_mgmt_hdr, 8 B); the header's cookie field
carries the TX slot (find_first_zero_bit), freed on the WMI_TXSTATUS completion. [SRC] htc_drv_txrx.c."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import struct
import threading
import time
from types import SimpleNamespace

import pytest
import usb.core

from wifit3.chips.ar9271_v2 import bringup, chan, constants as C, tx
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.errors import SetChannelError

# 24-byte stubs (no IEs): ProbeReq (mgmt), Null (data), RTS (control).
PROBE = bytes.fromhex("4000" "0000" "ffffffffffff" "001122334455" "ffffffffffff" "0000")
NULL = bytes.fromhex("4801" "0000" "001122334455" "00aabbccddee" "001122334455" "0000")
RTS = bytes.fromhex("b400" "4e04" "001122334455" "00aabbccddee")


def test_is_data_frame_classification():
    assert tx.is_data_frame(NULL) is True             # FTYPE_DATA
    assert tx.is_data_frame(PROBE) is False            # FTYPE_MGMT
    assert tx.is_data_frame(RTS) is False              # FTYPE_CTL


def test_build_mgmt_tx_wire_bytes():
    out = tx.build_mgmt_tx(5, PROBE, slot=7)
    htc_payload_len = tx.TX_MGMT_HDR_LEN + len(PROBE)              # 8 + 24 = 32
    expected = (struct.pack("<HH", tx.HTC_FRAME_HDR_LEN + htc_payload_len, tx.HIF_TX_STREAM_TAG)
                + struct.pack(">BBH4x", 5, 0, htc_payload_len)
                + bytes([0, 0, 0, 0, tx.ATH9K_KEY_TYPE_CLEAR, tx.ATH9K_TXKEYIX_INVALID, 7, 0])
                + PROBE)
    assert out == expected
    assert out[18] == 7                                            # cookie at mgmt offset


def test_build_data_tx_wire_bytes():
    out = tx.build_data_tx(6, NULL, slot=2)
    htc_payload_len = tx.TX_FRAME_HDR_LEN + len(NULL)             # 12 + 24 = 36
    expected = (struct.pack("<HH", tx.HTC_FRAME_HDR_LEN + htc_payload_len, tx.HIF_TX_STREAM_TAG)
                + struct.pack(">BBH4x", 6, 0, htc_payload_len)
                + struct.pack(">BBBBIBBBB", tx.ATH9K_HTC_NORMAL, 0, 0, 0, 0,
                              tx.ATH9K_KEY_TYPE_CLEAR, tx.ATH9K_TXKEYIX_INVALID, 2, 0)
                + NULL)
    assert out == expected
    assert out[22] == 2                                            # cookie at data offset


def test_tx_slots_find_first_zero_and_clear():
    htc_tx = tx.Ath9kHtcTx([5, 6])
    assert [htc_tx.get_slot() for _ in range(4)] == [0, 1, 2, 3]
    htc_tx.clear_slot(1)
    assert htc_tx.get_slot() == 1                                  # lowest free reused
    assert htc_tx.get_slot() == 4                                  # then the next fresh slot


def test_tx_slots_allocate_unique_slots_concurrently():
    htc_tx = tx.Ath9kHtcTx([5, 6])
    with ThreadPoolExecutor(max_workers=32) as executor:
        allocated = list(executor.map(lambda _: htc_tx.get_slot(), range(128)))
    assert sorted(allocated) == list(range(128))


def _sent(htc_tx, epid):
    slot = htc_tx.get_slot()
    htc_tx.txep(epid, slot)
    return slot


def _txstatus(*statuses):
    # wmi_event_txstatus: u8 cnt, then cnt x (cookie, ts_rate with the epid in bits 4-7, ts_flags).
    return bytes([len(statuses)]) + b"".join(bytes([cookie, epid << 4, 0]) for cookie, epid in statuses)


def test_txstatus_frees_the_slot_of_the_matching_frame():
    # ath9k_htc_txstatus -> ath9k_htc_tx_get_packet (cookie + epid) -> tx_process [SRC] htc_drv_txrx.c:618-680
    htc_tx = tx.Ath9kHtcTx([5, 6])
    mgmt, data = _sent(htc_tx, 5), _sent(htc_tx, 6)
    htc_tx.txstatus(_txstatus((data, 6)))
    assert htc_tx.tx_slot[mgmt] == 1 and htc_tx.tx_slot[data] == 0


def test_txstatus_for_another_endpoint_is_kept_pending():
    htc_tx = tx.Ath9kHtcTx([5, 6])
    slot = _sent(htc_tx, 5)
    htc_tx.txstatus(_txstatus((slot, 6)))
    assert htc_tx.tx_slot[slot] == 1
    assert htc_tx.pending_tx_events == [tx.TxEvent(slot, 6)]


def test_txstatus_for_an_unknown_endpoint_logs_invalid_epid(caplog):
    # get_htc_epid_queue: ath_err "Invalid EPID" [SRC] htc_drv_txrx.c:130-152
    htc_tx = tx.Ath9kHtcTx([5, 6])
    htc_tx.txstatus(_txstatus((0, 9)))
    assert "Invalid EPID: 9" in caplog.text
    assert "No matching packet" not in caplog.text


def test_txstatus_over_htc_max_tx_status_is_ignored():
    # WARN_ON_ONCE(txs->cnt > HTC_MAX_TX_STATUS) [SRC] htc_drv_txrx.c:655-656
    htc_tx = tx.Ath9kHtcTx([5, 6])
    slot = _sent(htc_tx, 5)
    htc_tx.txstatus(bytes([tx.HTC_MAX_TX_STATUS + 1]) + _txstatus((slot, 5))[1:] * 13)
    assert htc_tx.tx_slot[slot] == 1


def test_txstatus_over_htc_max_tx_status_warns_once(caplog):
    htc_tx = tx.Ath9kHtcTx([5, 6])
    htc_tx.txstatus(bytes([tx.HTC_MAX_TX_STATUS + 1]))
    htc_tx.txstatus(bytes([tx.HTC_MAX_TX_STATUS + 1]))
    assert caplog.text.count("txstatus cnt 13 > HTC_MAX_TX_STATUS") == 1


def test_a_short_txstatus_event_reads_zeros_past_its_end():
    htc_tx = tx.Ath9kHtcTx([5, 6])
    slot = _sent(htc_tx, 5)
    htc_tx.txstatus(bytes([2]) + _txstatus((slot, 5))[1:])
    assert htc_tx.tx_slot[slot] == 0
    assert htc_tx.pending_tx_events == [tx.TxEvent(0, 0)]


def test_cleanup_timer_matches_a_status_that_came_before_its_frame():
    # ath9k_htc_tx_cleanup_timer [SRC] htc_drv_txrx.c:761-807
    htc_tx = tx.Ath9kHtcTx([5, 6])
    htc_tx.txstatus(_txstatus((0, 5)))
    slot = _sent(htc_tx, 5)
    htc_tx.cleanup_timer()
    assert htc_tx.tx_slot[slot] == 0
    assert htc_tx.pending_tx_events == []


def test_cleanup_timer_drops_a_pending_status_after_40_ticks():
    htc_tx = tx.Ath9kHtcTx([5, 6])
    htc_tx.txstatus(_txstatus((7, 5)))
    for _ in range(tx.ATH9K_HTC_TX_TIMEOUT_COUNT - 1):
        htc_tx.cleanup_timer()
    assert len(htc_tx.pending_tx_events) == 1
    htc_tx.cleanup_timer()
    assert htc_tx.pending_tx_events == []


def test_cleanup_timer_frees_a_frame_with_no_status_after_3_s(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(tx.time, "monotonic", lambda: clock[0])
    htc_tx = tx.Ath9kHtcTx([5, 6])
    slot = _sent(htc_tx, 5)
    clock[0] += tx.ATH9K_HTC_TX_TIMEOUT_INTERVAL / 1000
    htc_tx.cleanup_timer()
    assert htc_tx.tx_slot[slot] == 1
    clock[0] += 0.001
    htc_tx.cleanup_timer()
    assert htc_tx.tx_slot[slot] == 0


def test_tx_drain_frees_every_queued_frame_and_drops_pending_statuses():
    # ath9k_htc_tx_drain [SRC] htc_drv_txrx.c:540-576
    htc_tx = tx.Ath9kHtcTx([5, 6])
    slots = [_sent(htc_tx, 5), _sent(htc_tx, 6)]
    htc_tx.txstatus(_txstatus((9, 5)))
    htc_tx.tx_drain()
    assert all(htc_tx.tx_slot[slot] == 0 for slot in slots)
    assert htc_tx.pending_tx_events == []


def _replay_driver(sent):
    transport = SimpleNamespace(wlan_out=lambda data: sent.append(bytes(data)) or len(data))
    wmi = SimpleNamespace(t=transport)
    endpoints = {svc: i + 1 for i, svc in enumerate(C.SERVICE_CONNECT_ORDER)}
    return AR9271V2Driver.for_replay(wmi, None, endpoints)


def test_inject_frame_routes_and_allocates_slots():
    # _emit_frame is the sync core (the public inject_frame is the async UI wrapper around it).
    sent: list[bytes] = []
    drv = _replay_driver(sent)

    assert drv._emit_frame(PROBE) == 0                             # mgmt, slot 0
    assert drv._emit_frame(NULL) == 1                              # data, slot 1
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)
    assert sent[1] == tx.build_data_tx(6, NULL, 1)

    # A TX status for slot 0 frees it; the next inject reuses it.
    drv.wmi_event_tasklet(tx.WMI_TXSTATUS_EVENTID, bytes([1, 0, 0x50, 0x01]))
    assert drv._emit_frame(RTS) == 0                               # control -> mgmt, slot 0 reused
    assert sent[2] == tx.build_mgmt_tx(5, RTS, 0)


def _driver_with_wlan_out(wlan_out):
    wmi = SimpleNamespace(t=SimpleNamespace(wlan_out=wlan_out))
    endpoints = {svc: i + 1 for i, svc in enumerate(C.SERVICE_CONNECT_ORDER)}
    return AR9271V2Driver.for_replay(wmi, None, endpoints)


def test_failed_usb_submission_drops_frame_and_releases_slot():
    # ath9k_htc_tx: tx_start fails -> "Tx failed", clear the slot, drop [SRC] htc_drv_main.c:891-904
    attempts = 0
    sent: list[bytes] = []

    def wlan_out(data):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise usb.core.USBTimeoutError("Operation timed out")
        sent.append(bytes(data))
        return len(data)

    drv = _driver_with_wlan_out(wlan_out)

    assert drv._emit_frame(PROBE) is None
    assert drv._emit_frame(PROBE) == 0
    assert sent == [tx.build_mgmt_tx(5, PROBE, 0)]


async def test_inject_frame_drops_frame_on_usb_error():
    # Same path through the public inject: the C's ath9k_htc_tx returns void [SRC] htc_drv_main.c:862
    def wlan_out(data):
        raise usb.core.USBError("Input/Output Error", errno=5)

    drv = _driver_with_wlan_out(wlan_out)

    await drv.inject_frame(PROBE)
    assert drv.htc_tx.get_slot() == 0


def test_device_gone_submission_propagates_and_releases_slot():
    def wlan_out(data):
        raise usb.core.USBError("No such device", errno=19)

    drv = _driver_with_wlan_out(wlan_out)

    try:
        drv._emit_frame(PROBE)
    except usb.core.USBError:
        pass
    else:
        raise AssertionError("device-gone USBError did not propagate")
    assert drv.htc_tx.get_slot() == 0


def test_non_usb_submission_error_propagates_and_releases_slot():
    def wlan_out(data):
        raise OSError("USB submission failed")

    drv = _driver_with_wlan_out(wlan_out)

    with pytest.raises(OSError, match="USB submission failed"):
        drv._emit_frame(PROBE)
    assert drv.htc_tx.get_slot() == 0


def test_no_free_tx_slot_drops_frame():
    # ath9k_htc_tx_get_slot -ENOBUFS -> "No free TX slot", drop [SRC] htc_drv_txrx.c:85-88,
    # htc_drv_main.c:885-888
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    for _ in range(tx.MAX_TX_BUF_NUM):
        drv.htc_tx.get_slot()

    assert drv._emit_frame(PROBE) is None
    assert sent == []


async def test_inject_frame_is_async_and_sends_the_frame():
    # The Driver contract the UI awaits: async, and actually sends the frame
    # (regression guard for the "object int can't be used in 'await' expression" crash).
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    await drv.inject_frame(PROBE)
    assert sent[0] == tx.build_mgmt_tx(5, PROBE, 0)


def test_fast_channel_change_falls_back_when_chip_is_not_alive(mocker):
    wmi = SimpleNamespace(cmd_or_none=mocker.Mock())
    hw = SimpleNamespace(getnf=mocker.Mock(), check_alive=mocker.Mock(return_value=False))
    channel = chan.channel_2ghz(6)
    full = mocker.patch.object(bringup, "_full_channel_change_stopped", return_value=0)
    restart = mocker.patch.object(bringup, "_restart_after_channel_change")

    bringup.fast_channel_change(wmi, hw, channel)

    full.assert_called_once_with(wmi, hw, channel)
    restart.assert_called_once_with(wmi, hw)


async def test_scan_tune_uses_fast_channel_change(mocker):
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    drv.hw = SimpleNamespace(curchan=chan.channel_2ghz(1))
    fast = mocker.patch.object(bringup, "fast_channel_change")
    full = mocker.patch.object(bringup, "full_channel_change")

    assert await drv.set_channel(6, scan=True) is None

    fast.assert_called_once()
    full.assert_not_called()


async def test_settled_tune_uses_full_channel_change(mocker):
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    drv.hw = SimpleNamespace(curchan=chan.channel_2ghz(1))
    fast = mocker.patch.object(bringup, "fast_channel_change")
    full = mocker.patch.object(bringup, "full_channel_change")

    assert await drv.set_channel(6, scan=False) is None

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
    endpoints = {svc: i + 1 for i, svc in enumerate(C.SERVICE_CONNECT_ORDER)}
    drv = AR9271V2Driver.for_replay(wmi, object(), endpoints)
    mocker.patch.object(bringup, "full_channel_change", side_effect=tracker.operation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tune = executor.submit(drv._do_channel_change, chan.channel_2ghz(1), False)
        emit = executor.submit(drv._emit_frame, PROBE)
        tune.result()
        emit.result()

    assert tracker.max_active == 1


def test_failed_tune_stops_tx_until_a_tune_succeeds(mocker, caplog):
    # htc_stop in ath9k_htc_tx_drain; only a successful tune reaches htc_start
    # [SRC] htc_drv_main.c:264, :305; hif_usb.c:384-387, :432, :445
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    caplog.set_level(logging.DEBUG, logger="wifit3.chips.ar9271_v2.driver")
    mocker.patch.object(bringup, "full_channel_change", side_effect=SetChannelError("injected"))
    with pytest.raises(SetChannelError):
        drv._do_channel_change(chan.channel_2ghz(6), False)
    assert "Unable to set channel" in caplog.text

    assert drv._emit_frame(PROBE) is None
    assert sent == []
    assert "Tx failed" in caplog.text

    mocker.patch.object(bringup, "full_channel_change")
    drv._do_channel_change(chan.channel_2ghz(6), False)
    assert drv._emit_frame(PROBE) == 0
    assert sent == [tx.build_mgmt_tx(5, PROBE, 0)]


def test_tune_drains_queued_frames_and_rearms_the_cleanup_timer_only_on_success(mocker):
    # timer_delete_sync + ath9k_htc_tx_drain before the reset, mod_timer after htc_start
    # [SRC] htc_drv_main.c:263-264, :311
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    drv.cleanup_timer_armed = True
    slot = drv._emit_frame(PROBE)
    mocker.patch.object(bringup, "full_channel_change", side_effect=SetChannelError("injected"))
    with pytest.raises(SetChannelError):
        drv._do_channel_change(chan.channel_2ghz(6), False)
    assert drv.htc_tx.tx_slot[slot] == 0
    assert drv.cleanup_timer_armed is False

    mocker.patch.object(bringup, "full_channel_change")
    drv._do_channel_change(chan.channel_2ghz(6), False)
    assert drv.cleanup_timer_armed is True


def test_txstatus_event_from_the_wmi_reaches_the_tx_slots():
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    slot = drv._emit_frame(NULL)
    drv.wmi.event_tasklet(tx.WMI_TXSTATUS_EVENTID, bytes([1, slot, 6 << 4, 0]))
    assert drv.htc_tx.tx_slot[slot] == 0


def test_tx_during_a_tune_is_dropped_without_waiting(mocker):
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    tuning = threading.Event()
    release = threading.Event()

    def tune(*args):
        tuning.set()
        release.wait(5)

    mocker.patch.object(bringup, "full_channel_change", side_effect=tune)
    with ThreadPoolExecutor(max_workers=2) as executor:
        tune_future = executor.submit(drv._do_channel_change, chan.channel_2ghz(6), False)
        assert tuning.wait(5)
        emit = executor.submit(drv._emit_frame, PROBE)
        assert emit.result(timeout=1) is None
        release.set()
        tune_future.result()
    assert sent == []


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
    endpoints = {svc: i + 1 for i, svc in enumerate(C.SERVICE_CONNECT_ORDER)}
    drv = AR9271V2Driver.for_replay(wmi, None, endpoints)

    await asyncio.gather(*(drv.inject_frame(PROBE) for _ in range(32)))
    assert transport.max_active == 1


async def test_inject_frame_slots_recycle_on_txstatus_past_bitmap():
    # The slot is held until its WMI_TXSTATUS frees it. Regression for the 256-slot leak that jammed
    # high-rate WEP replay/chopchop on AR9271 with ENOBUFS.
    sent: list[bytes] = []
    drv = _replay_driver(sent)
    n = tx.MAX_TX_BUF_NUM + 10
    for _ in range(n):
        await drv.inject_frame(PROBE)
        cookie = sent[-1][18]
        drv.wmi_event_tasklet(tx.WMI_TXSTATUS_EVENTID, bytes([1, cookie, 5 << 4, 0]))
    assert len(sent) == n