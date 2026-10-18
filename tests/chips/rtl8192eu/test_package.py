"""RTL8192EU package surface: VID:PID claims, lazy import, driver class attrs."""
from __future__ import annotations

from wifit3.chips import rtl8192eu
from wifit3.chips.driver import Driver, FakeMacSupport
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver


EXPECTED_IDS = {
    (0x0BDA, 0x818B),
    (0x2357, 0x0108),
    (0x2001, 0x3319),
    (0x2357, 0x0109),
    (0x2357, 0x0107),
    (0x2019, 0xAB33),
    (0x0BDA, 0x818C),
    (0x2001, 0x3312),
    (0x2357, 0x0126),
    (0x2C4E, 0x0100),
    (0x2C4E, 0x0104),
}


def test_supported_ids_are_the_8192eu_fops_ids():
    assert {(d.vid, d.pid) for d in rtl8192eu.SUPPORTED_IDS} == EXPECTED_IDS
    assert len(rtl8192eu.SUPPORTED_IDS) == 11


def test_every_id_is_rtl8192eu_silicon():
    assert {d.chipset for d in rtl8192eu.SUPPORTED_IDS} == {"RTL8192EU"}
    assert all(d.product_name for d in rtl8192eu.SUPPORTED_IDS)


def test_tp_link_tl_wn823n_v2_v3_is_claimed():
    entry = next(
        d for d in rtl8192eu.SUPPORTED_IDS if (d.vid, d.pid) == (0x2357, 0x0109)
    )
    assert entry.chipset == "RTL8192EU"
    assert entry.vendor is None          # brand is inlined in product_name
    assert entry.product_name == "TL-WN823N v2/v3"


def test_import_driver_returns_the_8192eu_class():
    assert rtl8192eu.import_driver() is RTL8192EUDriver
    assert issubclass(RTL8192EUDriver, Driver)


def test_supported_channels_are_2g_1_to_14():
    assert RTL8192EUDriver.SUPPORTED_CHANNELS == list(range(1, 15))


def test_active_monitor_is_spoofable():
    assert RTL8192EUDriver.FAKE_MAC is FakeMacSupport.SPOOFABLE
