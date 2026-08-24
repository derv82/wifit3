"""Unit tests for the interface Lease: config on enter, restore on exit."""
from wifit3.wlan.lease import Lease, SPOOFABLE


class FakeIface:
    def __init__(self, channel=1, armed="02:aa:bb:cc:dd:ee"):
        self.current_channel = channel
        self._armed = armed
        self.calls = []

    async def set_channel(self, ch, scan=False):
        self.current_channel = ch
        self.calls.append(("set_channel", ch))
        return True

    async def set_fake_mac(self, mac=None, bssid=None):
        self.calls.append(("set_fake_mac", mac, bssid))
        return self._armed

    async def clear_fake_mac(self):
        self.calls.append(("clear_fake_mac",))

    async def enable_rx_acks(self):
        self.calls.append(("enable_rx_acks",))

    async def disable_rx_acks(self):
        self.calls.append(("disable_rx_acks",))


class FakeArray:
    def __init__(self):
        self.own = set()

    def register_own_mac(self, mac):
        self.own.add(mac.lower())
        return mac.lower()

    def unregister_own_mac(self, mac):
        self.own.discard(mac.lower())


async def test_lease_arms_and_restores():
    iface, arr = FakeIface(channel=1), FakeArray()
    async with Lease(arr, iface, channel=6, fake_mac=SPOOFABLE,
                     bssid="aa:bb:cc:dd:ee:ff", ack_tally=True) as got:
        assert got is iface
        assert iface.current_channel == 6
        assert "02:aa:bb:cc:dd:ee" in arr.own
        assert ("enable_rx_acks",) in iface.calls
    assert iface.current_channel == 1              # channel restored
    assert arr.own == set()                        # own MAC released
    assert ("clear_fake_mac",) in iface.calls
    assert ("disable_rx_acks",) in iface.calls


async def test_lease_without_fake_mac_never_touches_own():
    iface, arr = FakeIface(channel=6), FakeArray()
    async with Lease(arr, iface, channel=6) as got:
        assert got is iface
    assert arr.own == set()
    assert not any(c[0] == "set_fake_mac" for c in iface.calls)


async def test_lease_channel_only_restores_original():
    iface, arr = FakeIface(channel=3), FakeArray()
    async with Lease(arr, iface, channel=11):
        assert iface.current_channel == 11
    assert iface.current_channel == 3


async def test_lease_registers_concrete_mac_when_arming_fails():
    iface, arr = FakeIface(armed=None), FakeArray()
    async with Lease(arr, iface, fake_mac="02:11:22:33:44:55") as got:
        assert got is iface
        assert "02:11:22:33:44:55" in arr.own          # still transmit from it, un-ACKed
    assert arr.own == set()


async def test_lease_spoofable_arm_failure_registers_nothing():
    iface, arr = FakeIface(armed=None), FakeArray()
    async with Lease(arr, iface, fake_mac=SPOOFABLE):
        assert arr.own == set()


async def test_lease_rearm_swaps_the_armed_mac():
    iface, arr = FakeIface(armed=None), FakeArray()          # un-ACKed: own == requested
    lease = Lease(arr, iface, fake_mac="02:aa:aa:aa:aa:01")
    async with lease:
        assert arr.own == {"02:aa:aa:aa:aa:01"}
        await lease.rearm("02:bb:bb:bb:bb:02")
        assert arr.own == {"02:bb:bb:bb:bb:02"}              # old released, new registered
        assert lease.mac == "02:bb:bb:bb:bb:02"
    assert arr.own == set()                                  # released on exit
