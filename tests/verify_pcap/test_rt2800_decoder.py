from __future__ import annotations

import asyncio
import struct
from dataclasses import replace

from tests.verify_pcap.capture_replay import CaptureDivergence, CaptureReplay
from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.virtual_clock_loop import MAIN_CALLER, VirtualClockEventLoop, current_caller_name
from wifit3.chips.rt5572 import constants as RT5572

FRAME = bytes(range(24))
# rf_vals_5592_xtal40 channel 36: N=86, K=4 (rt2800lib.c:11644+).
RF55XX_CHANNEL_36_RFCSR_VALUES = {36: (86, 4)}
# TX_STA_FIFO: VALID, PID_QUEUE 0.
VALID_STATUS = 0x29
EMPTY_STATUS = 0x28


def register_access(request: int, address: int, value: int) -> UsbOp:
    request_type = 0xC0 if request == RT5572.USB_MULTI_READ else 0x40
    setup = struct.pack("<BBHHH", request_type, request, 0, address, 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=request_type & 0x80,
                 transfer_type=TransferType.CONTROL, setup=setup, data=value.to_bytes(4, "little"), status=0,
                 submit_time=0.0, completion_time=0.0)


def rfcsr_write(register: int, value: int) -> UsbOp:
    return register_access(RT5572.USB_MULTI_WRITE, RT5572.RF_CSR_CFG,
                           RT5572.RF_CSR_CFG_BUSY | RT5572.RF_CSR_CFG_WRITE | register << 8 | value)


RF55XX_CHANNEL_36_TUNE = [
    register_access(RT5572.USB_MULTI_READ, RT5572.MAC_SYS_CTRL, RT5572.MAC_SYS_CTRL_ENABLE_TX
                    | RT5572.MAC_SYS_CTRL_ENABLE_RX),
    register_access(RT5572.USB_MULTI_WRITE, RT5572.MAC_SYS_CTRL, RT5572.MAC_SYS_CTRL_ENABLE_TX),
    register_access(RT5572.USB_MULTI_READ, RT5572.CH_IDLE_STA, 0x0),
    rfcsr_write(8, 86),
    rfcsr_write(9, RT5572.RFCSR9_MOD | 4),
]


def tx_op(txwi_size: int) -> UsbOp:
    txwi_word1 = len(FRAME) << 16
    data = struct.pack("<3I", 0, 0, txwi_word1) + bytes(txwi_size - 8) + FRAME + bytes(4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x01, transfer_type=TransferType.BULK,
                 setup=None, data=data, status=0, submit_time=0.0, completion_time=0.0)


def test_channel_change_looks_up_the_given_rfcsr_values() -> None:
    decoder = Rt2800Decoder(channel_rf3_mask=RT5572.RFCSR9_K | RT5572.RFCSR9_N,
                            channel_rfcsr_values=RF55XX_CHANNEL_36_RFCSR_VALUES)
    assert decoder.channel_change_at(RF55XX_CHANNEL_36_TUNE, 0) == 36


def test_channel_change_default_reads_only_the_rf_vals_3x_2ghz_tune() -> None:
    assert Rt2800Decoder().channel_change_at(RF55XX_CHANNEL_36_TUNE, 0) is None


def test_injected_frame_follows_the_given_txwi_size() -> None:
    decoder = Rt2800Decoder(txwi_size=RT5572.TXWI_DESC_SIZE_5WORDS)
    assert decoder.injected_frame_at([tx_op(RT5572.TXWI_DESC_SIZE_5WORDS)], 0) == FRAME


def test_injected_frame_default_txwi_is_4_words() -> None:
    assert Rt2800Decoder().injected_frame_at([tx_op(RT5572.TXWI_DESC_SIZE_4WORDS)], 0) == FRAME


def tx_sta_fifo_read(value: int, submit_time: float) -> UsbOp:
    return replace(register_access(RT5572.USB_MULTI_READ, RT5572.TX_STA_FIFO, value), submit_time=submit_time,
                   completion_time=submit_time + 0.0001)


def replay_tx_status_task(ops: list[UsbOp], sleeps_before_reads: list[float],
                          replies: list[UsbOp | None] | None = None) -> CaptureReplay:
    """An hrtimer task makes one TX_STA_FIFO read after each sleep."""
    async def tx_status_task() -> None:
        for sleep in sleeps_before_reads:
            await asyncio.sleep(sleep)
            reply = replay.match_request(replace(ops[0], data=b""), current_caller_name())
            if replies is not None:
                replies.append(reply)

    async def main() -> None:
        try:
            await loop.create_task(tx_status_task())
        except CaptureDivergence:
            pass

    loop = VirtualClockEventLoop(0.0, lambda: True, frozenset({tx_status_task.__qualname__}))
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None,
                           Rt2800Decoder().is_racing_extra_request, loop)
    try:
        loop.run_until_complete(loop.create_task(main(), name=MAIN_CALLER))
    finally:
        loop.close()
    return replay


def test_tx_status_extra_poll_after_a_valid_then_empty_read_is_accepted() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(VALID_STATUS, 0.5)]
    replies: list[UsbOp | None] = []
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001], replies)
    assert replay.divergence is None
    assert replay.racing_extra_request_count == 1
    assert replies[2] is ops[1]


def test_tx_status_extra_poll_in_the_capture_is_reproduced() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(EMPTY_STATUS, 0.0012)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001])
    assert replay.divergence is None
    assert replay.unreproduced_positions() == []
    assert replay.racing_extra_request_count == 0


def test_tx_status_missing_extra_poll_is_not_accepted() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(EMPTY_STATUS, 0.0012)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001])
    assert replay.unreproduced_positions() == [2]


def test_tx_status_extra_poll_after_a_valid_read_is_not_accepted() -> None:
    ops = [tx_sta_fifo_read(EMPTY_STATUS, 0.0), tx_sta_fifo_read(VALID_STATUS, 0.0001),
           tx_sta_fifo_read(VALID_STATUS, 0.5)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001])
    assert replay.divergence is not None and "timer fired" in replay.divergence.reason


def test_tx_status_second_extra_poll_is_not_accepted() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(VALID_STATUS, 0.5)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001, 0.001])
    assert replay.divergence is not None and "timer fired" in replay.divergence.reason


def test_tx_status_read_after_an_extra_poll_may_be_early_by_the_extra_polls_delay() -> None:
    # Linux made no extra poll; a TX DMA done in its 1 ms window armed the 2 ms hrtimer (rt2800usb.c:143-153).
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(EMPTY_STATUS, 0.0034)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001, 0.001])
    assert replay.divergence is None
    assert replay.unreproduced_positions() == []
    assert replay.racing_extra_request_count == 1


def test_tx_status_read_after_an_extra_poll_earlier_than_its_delay_is_not_accepted() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(EMPTY_STATUS, 0.0045)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001, 0.001])
    assert replay.divergence is not None and "timer fired" in replay.divergence.reason


def test_tx_status_extra_polls_delay_applies_to_one_read() -> None:
    ops = [tx_sta_fifo_read(VALID_STATUS, 0.0), tx_sta_fifo_read(EMPTY_STATUS, 0.0001),
           tx_sta_fifo_read(EMPTY_STATUS, 0.0025), tx_sta_fifo_read(EMPTY_STATUS, 0.0049)]
    replay = replay_tx_status_task(ops, [0.0, 0.0001, 0.001, 0.001, 0.001])
    assert replay.divergence is not None and replay.divergence.position == 3
