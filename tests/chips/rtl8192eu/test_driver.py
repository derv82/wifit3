"""RTL8192EUDriver active monitor (port 0 via core.c set_linktype / set_mac / set_bssid)."""
from unittest.mock import MagicMock

from wifit3.chips.rtl8192eu import constants as C
from wifit3.chips.rtl8192eu.core import Nl80211Iftype
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver


class FakeTransport:
    def __init__(self, msr: int) -> None:
        self.msr = msr
        self.writes: list[tuple[int, int]] = []

    def read8(self, addr: int) -> int:
        return self.msr if addr == C.REG_MSR else 0

    def write8(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 1


async def test_enter_active_monitor_programs_port0():
    driver = RTL8192EUDriver(MagicMock(), MagicMock())
    transport = FakeTransport(msr=0x0C)
    driver.priv.transport = transport
    mac, bssid = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")

    assert await driver.enter_active_monitor(mac, bssid) == mac
    assert transport.writes[0] == (C.REG_MSR, 0x0C | C.MSR_LINKTYPE_STATION)
    assert transport.writes[1:7] == [(C.REG_MACID + i, mac[i]) for i in range(6)]
    assert transport.writes[7:13] == [(C.REG_BSSID + i, bssid[i]) for i in range(6)]

    transport.writes.clear()
    await driver.enter_active_monitor(mac, mac)
    assert transport.writes[0] == (C.REG_MSR, 0x0C | C.MSR_LINKTYPE_AP)
    assert driver.priv.vif_type == Nl80211Iftype.NL80211_IFTYPE_AP


async def test_exit_active_monitor_restores_the_silicon_mac():
    driver = RTL8192EUDriver(MagicMock(), MagicMock())
    transport = FakeTransport(msr=C.MSR_LINKTYPE_STATION)
    driver.priv.transport = transport
    driver.priv.mac_addr = bytes.fromhex("0011223344ee")
    driver._started = True

    await driver.exit_active_monitor()
    assert transport.writes[0] == (C.REG_MSR, C.MSR_LINKTYPE_NONE)
    assert transport.writes[1:7] == [(C.REG_MACID + i, driver.priv.mac_addr[i]) for i in range(6)]
    assert transport.writes[7:13] == [(C.REG_BSSID + i, 0) for i in range(6)]
    assert driver.priv.vif_type is None
