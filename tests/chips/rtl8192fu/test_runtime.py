from unittest.mock import Mock

from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.runtime import configure_monitor_filter, set_tx_led


def test_set_tx_led_uses_source_software_mode_for_both_led_pins():
    transport = Mock()
    transport.read32.return_value = 0

    set_tx_led(transport, True)

    assert transport.write32.call_args_list == [
        ((0x1038, 0x00020080),),
        ((0x103C, 0x001B0000),),
        ((0x004C, (1 << 21) | (1 << 3) | (1 << 11)),),
    ]


def test_configure_monitor_filter_accepts_other_bss_control_and_power_management():
    transport = Mock()

    configure_monitor_filter(transport)

    expected = 0x7000600E | C.RCR_ACCEPT_AP | C.RCR_ACCEPT_PM | C.RCR_ACCEPT_CTRL_FRAME
    transport.write32.assert_called_once_with(C.REG_RCR, expected)
