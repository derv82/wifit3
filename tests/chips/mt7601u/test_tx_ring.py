"""TX queue tests for the MT7601U port.

dma.c's mt7601u_dma_submit_tx is not a device-memory ring: it fills a pre-allocated
USB bulk URB, submits it, and advances q->end / q->used, which the completion callback
decrements. TxQueue ports that bookkeeping. Nothing here touches hardware.
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.constants import MT_TX_STAT_FIFO
from wifit3.chips.mt7601u.mcu import MT7601UMcu
from wifit3.chips.mt7601u.tx import (
    TX_QUEUE_COUNT,
    TX_QUEUE_INBAND_CMD,
    TX_QUEUE_INJECT,
    TX_NO_STATION,
)
from wifit3.chips.mt7601u.tx_ring import TX_QUEUE_ENTRIES, TX_STATUS_HISTORY, TxQueue

FRAME = b"\x08\x00" + b"\x00" * 30
"""A 32-byte data frame, FC 0x0008: 24 bytes of MAC header plus an 8-byte body."""

STATUS_SUCCESS = C.MT_TX_STAT_FIFO_VALID | C.MT_TX_STAT_FIFO_SUCCESS
STATUS_FAIL = C.MT_TX_STAT_FIFO_VALID
"""Two popped FIFO words: the silicon completed the transmit, with and without SUCCESS."""


class FakeTransport:
    """Records submitted URBs with the endpoint each went out on.

    The endpoint is the part worth recording: dma.c sends each queue on its own
    OUT pipe, so a frame on the wrong one is accepted by the chip and never
    modulated.
    """

    def __init__(self) -> None:
        self.submitted: list[bytes] = []
        self.endpoints: list[int] = []
        self.status_fifo: list[int] = []
        self.fail_next = False

    def bulk_out_tx(self, data: bytes, queue: int, timeout_ms: int = 0) -> int:
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("TX URB timed out")
        self.submitted.append(data)
        self.endpoints.append(queue)
        return len(data)

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        return self.bulk_out_tx(data, TX_QUEUE_INBAND_CMD, timeout_ms)

    bulk_out_inband_fw = bulk_out

    def rr(self, offset: int) -> int:
        """Pops the queued status word, or 0 (FIFO invalid) when there is none."""
        if offset != MT_TX_STAT_FIFO or not self.status_fifo:
            return 0
        return self.status_fifo.pop(0)

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        return b""


def make_queue(entries: int = TX_QUEUE_ENTRIES) -> tuple[TxQueue, FakeTransport]:
    tp = FakeTransport()
    mcu = MT7601UMcu(tp)
    mcu.mcu_running = True
    return TxQueue(tp, mcu, entries=entries), tp


class TestSizing:
    def test_defaults_to_the_kernel_entry_count(self) -> None:
        assert TX_QUEUE_ENTRIES == 64

    def test_starts_empty(self) -> None:
        q, _tp = make_queue()
        assert q.used == 0
        assert q.free_entries() == TX_QUEUE_ENTRIES

    def test_six_out_queues_are_named(self) -> None:
        assert TX_QUEUE_COUNT == 6


class TestSubmit:
    def test_a_submitted_frame_reaches_the_transport(self) -> None:
        q, tp = make_queue()
        assert q.submit(FRAME) is True
        assert len(tp.submitted) == 1

    def test_the_submitted_buffer_carries_the_descriptor(self) -> None:
        q, tp = make_queue()
        q.submit(FRAME)
        sent = tp.submitted[0]
        assert len(sent) % 4 == 0
        assert sent[-4:] == b"\x00\x00\x00\x00"

    def test_one_submit_releases_its_entry_immediately(self) -> None:
        """bulk_out_tx is synchronous, so the frame is done when submit returns and
        the slot must not stay occupied waiting for a status that may never come."""
        q, _tp = make_queue()
        q.submit(FRAME)
        assert q.used == 0
        assert q.free_entries() == TX_QUEUE_ENTRIES

    def test_the_fourth_slot_wraps_back_to_the_first(self) -> None:
        """end advances modulo entries, so freed slots are reused in order."""
        q, _tp = make_queue(entries=4)
        for _ in range(3):
            q.submit(FRAME)
        assert q.end == 3
        q.pump(3)
        q.submit(FRAME)
        assert q.end == 0

    def test_the_ring_wraps(self) -> None:
        q, _tp = make_queue(entries=4)
        for _ in range(4):
            q.submit(FRAME)
        q.pump(4)
        assert q.end == 0


class TestFullQueue:
    """dma.c returns -ENOSPC on a full queue. Unreachable while bulk_out_tx is
    synchronous -- submit releases each slot as it goes -- so these force the count
    to the cap rather than filling it, keeping the guard under test for the day an
    async transport makes it reachable again."""

    def test_a_full_queue_refuses_rather_than_overwriting(self) -> None:
        q, _tp = make_queue(entries=2)
        q.used = q.entries
        assert q.submit(FRAME) is False

    def test_a_refused_submit_sends_nothing(self) -> None:
        q, tp = make_queue(entries=1)
        q.used = q.entries
        assert q.submit(FRAME) is False
        assert tp.submitted == []

    def test_a_refused_submit_leaves_the_count_alone(self) -> None:
        q, _tp = make_queue(entries=1)
        q.used = q.entries
        q.submit(FRAME)
        assert q.used == q.entries


class TestFailures:
    def test_a_timed_out_transfer_reports_failure_not_success(self) -> None:
        """inject_frame_slow_retry polls RX for an ACK; a TX failure that looks
        like success makes it spin on an ACK that will never arrive."""
        q, tp = make_queue()
        tp.fail_next = True
        assert q.submit(FRAME) is False

    def test_a_timed_out_transfer_does_not_consume_an_entry(self) -> None:
        q, tp = make_queue()
        tp.fail_next = True
        q.submit(FRAME)
        assert q.used == 0

    def test_the_queue_is_usable_again_after_a_failure(self) -> None:
        q, tp = make_queue()
        tp.fail_next = True
        assert q.submit(FRAME) is False
        assert q.submit(FRAME) is True


class TestPump:
    def test_pump_releases_completed_entries(self) -> None:
        q, _tp = make_queue()
        q.used = 1
        assert q.pump(1) == 1
        assert q.used == 0
        assert q.free_entries() == TX_QUEUE_ENTRIES

    def test_pump_with_nothing_outstanding_releases_nothing(self) -> None:
        q, _tp = make_queue()
        assert q.pump(0) == 0
        assert q.used == 0

    def test_pump_reports_how_many_it_released(self) -> None:
        q, _tp = make_queue()
        q.used = 3
        assert q.pump(2) == 2
        assert q.used == 1


class TestClose:
    def test_close_empties_the_queue(self) -> None:
        q, _tp = make_queue()
        q.submit(FRAME)
        q.close()
        assert q.used == 0

    def test_a_closed_queue_refuses_submits(self) -> None:
        q, _tp = make_queue()
        q.close()
        assert q.submit(FRAME) is False


class TestRejectsBadFrames:
    @pytest.mark.parametrize("bad", [b"", b"\x08", b"\x08\x00"])
    def test_a_frame_too_short_to_be_80211_is_refused(self, bad: bytes) -> None:
        q, tp = make_queue()
        with pytest.raises(ValueError):
            q.submit(bad)
        assert tp.submitted == []

    def test_an_out_of_range_queue_is_refused(self) -> None:
        with pytest.raises(ValueError):
            TxQueue(FakeTransport(), MT7601UMcu(FakeTransport()), queue=9)

    def test_the_default_queue_is_the_endpoint_tx_c_derives(self) -> None:
        """Not the index named AC_BE: tx.c puts mac80211 queue 0 on endpoint 4."""
        q, _tp = make_queue()
        assert q.queue == TX_QUEUE_INJECT

    def test_a_submitted_frame_goes_out_on_its_own_endpoint(self) -> None:
        """dma.c:310 sends each queue on dev->out_eps[queue]. Sending a frame to
        the inband endpoint hands it to the MCU as a command instead."""
        q, tp = make_queue()
        assert q.submit(FRAME) is True
        assert tp.endpoints == [TX_QUEUE_INJECT]
        assert tp.endpoints != [TX_QUEUE_INBAND_CMD]

    def test_the_monitor_slot_is_the_default_wcid(self) -> None:
        q, _tp = make_queue()
        assert q.wcid == TX_NO_STATION


class TestStatusPump:
    """Every transmit must drain MT_TX_STAT_FIFO (dma.c:278 arms stat_work 10ms
    after each completion). The FIFO is read-pop, so an undrained one is why a TX
    result cannot be trusted."""

    def test_a_submit_pops_the_status_the_mac_queued(self) -> None:
        q, tp = make_queue()
        tp.status_fifo = [STATUS_SUCCESS]
        q.submit(FRAME)
        assert tp.status_fifo == []          # read-pop, not a peek
        assert q.statuses[0].success is True

    def test_a_status_is_recorded_without_driving_slot_accounting(self) -> None:
        """submit already released the slot on the synchronous write; a popped status
        feeds tx_statuses() and must not retire anything on top of that."""
        q, tp = make_queue()
        tp.status_fifo = [STATUS_SUCCESS]
        q.submit(FRAME)
        assert q.used == 0
        assert q.statuses[0].success is True

    def test_a_failed_status_does_not_leak_the_slot_either(self) -> None:
        """A frame the silicon dropped still finished, so its slot must not leak."""
        q, tp = make_queue()
        tp.status_fifo = [STATUS_FAIL]
        q.submit(FRAME)
        assert q.used == 0
        assert q.statuses[0].success is False

    def test_an_empty_fifo_does_not_strand_the_entry(self) -> None:
        """The FIFO is a free-running counter, so most frames never get a status.
        Retiring slots against it ratcheted used to the cap and refused every frame
        after ~65 injects, measured on hardware with the endpoint still accepting."""
        q, tp = make_queue()
        q.submit(FRAME)
        assert q.used == 0
        assert q.statuses == []

    def test_several_statuses_backed_up_at_once_are_all_popped(self) -> None:
        q, tp = make_queue()
        tp.status_fifo = [STATUS_SUCCESS, STATUS_FAIL, STATUS_SUCCESS]
        statuses = q.collect_status()
        assert [s.success for s in statuses] == [True, False, True]
        assert tp.status_fifo == []

    def test_draining_stops_at_the_cap(self) -> None:
        """A wedged MAC may never report the FIFO empty, so the loop is bounded
        rather than trusting an invalid read to end it."""
        q, tp = make_queue()
        tp.status_fifo = [STATUS_SUCCESS] * 10
        assert len(q.collect_status(limit=3)) == 3

    def test_the_history_is_bounded(self) -> None:
        """Unbounded status on a long-running scanner would grow without limit."""
        q, tp = make_queue()
        for _ in range(TX_STATUS_HISTORY + 20):
            tp.status_fifo.append(STATUS_SUCCESS)
            q.collect_status()
        assert len(q.statuses) == TX_STATUS_HISTORY

    def test_the_ack_bit_is_recorded(self) -> None:
        q, tp = make_queue()
        tp.status_fifo = [STATUS_SUCCESS | C.MT_TX_STAT_FIFO_ACKREQ]
        assert q.collect_status()[0].ack_requested is True

    def test_the_wcid_and_rate_are_recorded(self) -> None:
        q, tp = make_queue()
        tp.status_fifo = [(0x33 << 8) | (7 << 16) | C.MT_TX_STAT_FIFO_VALID]
        entry = q.collect_status()[0]
        assert entry.wcid == 0x33
        assert entry.rate == 7
