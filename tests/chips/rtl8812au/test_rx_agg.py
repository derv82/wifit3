"""RX-DMA aggregation arming.

The 8812a leaves the USB RX-DMA page accumulator un-armed at the FW default,
which wedges RX after a few seconds (clean cliff, control plane alive). We port
``rtw_usb_dynamic_rx_agg_v2(enable=false)`` and arm it once at attach. These pin
the WIRE-confirmed register contract.
"""
from unittest.mock import MagicMock

from wifit3.chips.rtl8812au import constants as C
from wifit3.chips.rtl8812au import mac


def test_configure_rx_aggregation_writes_kernel_monitor_values():
    t = MagicMock()
    mac.configure_rx_aggregation(t)
    # size=0, timeout=1 → 0x0100. [WIRE captures_rtw88_8812au capture-1 frame 7649]
    t.write16.assert_called_once_with(C.REG_RXDMA_AGG_PG_TH, 0x0100)
    t.write8_set.assert_called_once_with(C.REG_TXDMA_PQ_MAP, C.BIT_RXDMA_AGG_EN)


def test_configure_rx_aggregation_log_false_is_quiet(caplog):
    """`log=False` arms the accumulator without emitting the info line."""
    mac.configure_rx_aggregation(MagicMock(), log=False)
    assert "aggregation armed" not in caplog.text
