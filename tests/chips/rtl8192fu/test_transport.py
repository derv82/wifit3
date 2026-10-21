from dataclasses import dataclass

import usb.util

from wifit3.chips.rtl8192fu.chip import identify_chip
from wifit3.chips.rtl8192fu.transport import RTL8192FUTransport


@dataclass
class Endpoint:
    bEndpointAddress: int
    bmAttributes: int


def test_parse_captured_an3u_endpoints():
    interface = [
        Endpoint(0x84, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x05, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x06, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x87, usb.util.ENDPOINT_TYPE_INTR),
        Endpoint(0x08, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x09, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x0C, usb.util.ENDPOINT_TYPE_BULK),
        Endpoint(0x0D, usb.util.ENDPOINT_TYPE_BULK),
    ]

    endpoints = RTL8192FUTransport.parse_usb(interface)

    assert endpoints.bulk_in == 0x84
    assert endpoints.interrupt_in == 0x87
    assert endpoints.bulk_out == (0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D)


def test_identify_chip_uses_captured_register_order_and_values(mocker):
    transport = mocker.Mock()
    transport.read32.side_effect = [0x0C481137, 0x00006804, 0x022429D5]
    transport.endpoints = mocker.Mock(bulk_out=(0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D))

    identity = identify_chip(transport)

    assert identity.chip_cut == 1
    assert identity.vendor == "UMC"
    assert identity.rom_rev == 0
    assert identity.has_wifi is True
    assert identity.has_bluetooth is False
    assert identity.has_gps is False
    assert identity.tx_queue_count == 3
    assert [call.args[0] for call in transport.read32.call_args_list] == [0x00F0, 0x0068, 0x00F4]
