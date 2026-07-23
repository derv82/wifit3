"""WlanInterface is a pure radio now (channel/hop/inject/deauth + raw RX fan-out); the 802.11
picture lives in WlanSink (see test_sink.py). These cover the radio behaviors: disconnect latching,
the hopper's device-gone guard, and the deauth frame/ACK bookkeeping."""
import asyncio

import usb.core

from wifit3.wlan.interface import WlanInterface


def test_on_device_lost_latches_and_fans_once(mocker):
    """The disconnect sink fires subscribers exactly once (latched) and trips the hop flag."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")
    iface._is_hopping = True
    seen = []
    iface.register_disconnect_callback(seen.append)

    first = usb.core.USBError("gone", errno=19)
    iface._on_device_lost(first)
    iface._on_device_lost(usb.core.USBError("again", errno=19))  # latched → ignored

    assert seen == [first]
    assert iface._device_lost is True
    assert iface._is_hopping is False


async def test_hopper_surfaces_device_gone_and_stops(mocker):
    """An unplug mid-hop: the hopper's tune raises device-gone, the guard routes it to the
    disconnect sink and stops hopping instead of killing the hop task with an unhandled raise."""
    driver = mocker.MagicMock()

    async def boom(channel, scan=False):
        raise usb.core.USBError("no dev", errno=19)   # LIBUSB_ERROR_NO_DEVICE

    driver.set_channel = boom
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")
    seen = []
    iface.register_disconnect_callback(seen.append)

    await iface.start_hopping(channels=[1], interval=0.01)
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)

    assert len(seen) == 1 and isinstance(seen[0], usb.core.USBError)
    assert iface._is_hopping is False
    await iface.stop_hopping()


async def test_deauth_sets_unicast_ack_nav(mocker):
    """A client-targeted deauth burst carries the unicast-ACK NAV (0x013A) in the duration
    of both spoofed frames: the destination (addr1) ACKs, so we reserve SIFS + a 1 Mbps
    ACK. Built in the shared interface path, so this holds for every driver."""
    driver = mocker.MagicMock()
    driver.inject_frame_slow_retry = mocker.AsyncMock(return_value=True)
    driver.enable_rx_acks = mocker.AsyncMock()
    driver.disable_rx_acks = mocker.AsyncMock()
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")

    await iface.deauth_client("aa:bb:cc:dd:ee:ff", "00:11:22:33:44:55", rounds=1)

    frames = [c.args[0] for c in driver.inject_frame_slow_retry.call_args_list]
    client_deauth, ap_deauth = frames[0], frames[1]
    # client_deauth addr1 = client (unicast) → NAV 0x013A (little-endian)
    assert client_deauth[4:10] == bytes.fromhex("001122334455")
    assert client_deauth[2:4] == b"\x3a\x01"
    # ap_deauth addr1 = AP (unicast) → NAV 0x013A
    assert ap_deauth[4:10] == bytes.fromhex("aabbccddeeff")
    assert ap_deauth[2:4] == b"\x3a\x01"


async def test_deauth_client_tallies_per_direction_acks(mocker):
    """deauth_client arms TX-ACK detection and returns a per-direction ACK tally: an
    AP→Client frame ACKed = the CLIENT heard us; a Client→AP frame ACKed = the AP heard us.
    Here the client ACKs every AP→Client frame but the AP ACKs none."""
    driver = mocker.MagicMock()
    driver.enable_rx_acks = mocker.AsyncMock()
    driver.disable_rx_acks = mocker.AsyncMock()
    # slow-retry returns True (ACKed) for the AP->Client frame (addr2 = AP), False otherwise.
    async def _inject(frame, max_resends=0):
        return frame[10:16] == bytes.fromhex("aabbccddeeff")   # addr2 = spoofed AP
    driver.inject_frame_slow_retry = mocker.AsyncMock(side_effect=_inject)
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")

    res = await iface.deauth_client("aa:bb:cc:dd:ee:ff", "00:11:22:33:44:55", rounds=4)

    driver.enable_rx_acks.assert_awaited_once()
    driver.disable_rx_acks.assert_awaited_once()
    assert res.measured and res.client_sent == 4 and res.ap_sent == 4
    assert res.client_acks == 4 and res.ap_acks == 0      # client heard us, AP silent
    assert res.total_acked == 4 and res.total_sent == 8


async def test_broadcast_deauth_only_group_frame_nav_zero(mocker):
    """'Deauth all' sends a single AP→ff:ff:ff:ff:ff:ff wave: a group address that is never
    ACKed, so NAV is 0. One direction only: there is NO reverse (broadcast→AP) frame: that
    would de-auth nobody. Broadcast is fire-and-forget (never arms TX-ACK detection)."""
    driver = mocker.MagicMock()
    driver.inject_frame = mocker.AsyncMock(return_value=True)
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")

    sent = await iface.deauth_broadcast("aa:bb:cc:dd:ee:ff", count=3)

    frames = [c.args[0] for c in driver.inject_frame.call_args_list]
    assert sent == 3 and len(frames) == 3                 # one direction, no reverse frame
    driver.enable_ack_detect.assert_not_called()
    for f in frames:
        assert f[4:10] == b"\xff\xff\xff\xff\xff\xff"      # addr1 = broadcast
        assert f[10:16] == bytes.fromhex("aabbccddeeff")   # addr2 = AP (spoofed source)
        assert f[2:4] == b"\x00\x00"                       # group dest → NAV 0
