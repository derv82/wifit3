from __future__ import annotations

import struct

from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from wifit3.chips.rt5572 import constants as RT5572

FRAME = bytes(range(24))
# rf_vals_5592_xtal40 channel 36: N=86, K=4 (rt2800lib.c:11644+).
RF55XX_CHANNEL_36_RFCSR_VALUES = {36: (86, 4)}


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
