from unittest.mock import Mock

from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.runtime import configure_monitor_filter


def test_configure_monitor_filter_accepts_other_bss_control_and_power_management():
    transport = Mock()

    configure_monitor_filter(transport)

    expected = 0x7000600E | C.RCR_ACCEPT_AP | C.RCR_ACCEPT_PM | C.RCR_ACCEPT_CTRL_FRAME
    transport.write32.assert_called_once_with(C.REG_RCR, expected)
