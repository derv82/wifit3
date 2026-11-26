"""RTL8822CU hardware TX LED tests."""
from unittest.mock import MagicMock

from wifit3.chips.rtl8822cu.led import disable_tx_blink, enable_tx_blink


def test_enable_tx_blink_selects_gpio8_then_the_hardware_tx_mode():
    transport = MagicMock()
    transport.read8.side_effect = [0xFF, 0xFF, 0xFF]

    enable_tx_blink(transport)

    assert transport.read8.call_args_list == [
        ((0x004A,),),
        ((0x004E,),),
        ((0x004E,),),
    ]
    assert transport.write8.call_args_list == [
        ((0x004A, 0xFC),),
        ((0x004E, 0xFF),),
        ((0x004E, 0xBC),),
    ]


def test_disable_tx_blink_changes_to_software_off_mode():
    transport = MagicMock()
    transport.read8.side_effect = [0xFF, 0x08]

    disable_tx_blink(transport)

    assert transport.write8.call_args_list == [
        ((0x004E, 0xB8),),
        ((0x004E, 0x08),),
    ]
