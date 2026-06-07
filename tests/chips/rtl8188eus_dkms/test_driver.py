"""Hardware-free regression for the RTL8188EUS (DKMS) driver orchestration.

Covers the WlanDriver surface that does not need live USB: the device IDs / channel set,
the stateful RfRegChnlVal threading through set_channel, the RX dispatch, the (not-yet-wired)
inject_frame, and the manager registration + env-var ordering. asyncio_mode=auto runs the
async tests without a decorator.
"""
from wifit3.chips.rtl8188eus_dkms import chan, driver as drv_mod
from wifit3.chips.rtl8188eus_dkms.constants import PID, VID
from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver


class _FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_supported_ids_and_channels():
    ids = Rtl8188eusDkmsDriver.SUPPORTED_IDS
    assert (ids[0].vid, ids[0].pid) == (VID, PID)
    assert Rtl8188eusDkmsDriver.SUPPORTED_CHANNELS == list(range(1, 14))


async def test_set_channel_threads_rfregchnlval(monkeypatch):
    drv = Rtl8188eusDkmsDriver(_FakeTransport())
    drv._tx_power = object()
    calls = []

    def fake_set_channel(t, txp, rf_chnl, ch):
        calls.append((rf_chnl, ch))
        return 0x07C00 | ch                      # stand-in updated RfRegChnlVal
    monkeypatch.setattr(chan, "set_channel", fake_set_channel)

    drv._rf_chnl = 0x07407
    assert await drv.set_channel(6) is True
    assert calls[0] == (0x07407, 6)
    assert drv._channel == 6 and drv._rf_chnl == (0x07C00 | 6)
    # the next tune threads the updated RfRegChnlVal (stateful across tunes).
    await drv.set_channel(11)
    assert calls[1][0] == (0x07C00 | 6)


def test_dispatch_parses_and_fans(monkeypatch):
    drv = Rtl8188eusDkmsDriver(_FakeTransport())
    got = []
    drv.register_rx_callback(got.append)
    monkeypatch.setattr(drv_mod, "iter_frames", lambda buf: [(b"frame", -50)])
    monkeypatch.setattr(drv_mod.WlanFrameParser, "parse_80211_frame",
                        staticmethod(lambda f, r: {"frame": f, "rssi": r}))
    drv._dispatch(b"aggregated-bulk-in")
    assert got == [{"frame": b"frame", "rssi": -50}]


def test_dispatch_without_callback_is_noop():
    drv = Rtl8188eusDkmsDriver(_FakeTransport())
    drv._dispatch(b"\x00" * 32)                  # no callback registered -> no crash


async def test_inject_frame_not_yet_wired():
    drv = Rtl8188eusDkmsDriver(_FakeTransport())
    assert await drv.inject_frame(b"\xc0\x00" + b"\x00" * 30) is False


def test_manager_registration_and_env_order(monkeypatch):
    from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
    from wifit3.wlan import manager

    monkeypatch.delenv("WIFIT3_RTL8188", raising=False)
    drivers = manager._all_drivers()
    assert RTL8188EUSDriver in drivers and Rtl8188eusDkmsDriver in drivers
    # default: mainline-derived port wins (lower index) for 2357:010c.
    assert drivers.index(RTL8188EUSDriver) < drivers.index(Rtl8188eusDkmsDriver)

    monkeypatch.setenv("WIFIT3_RTL8188", "dkms")
    drivers = manager._all_drivers()
    assert drivers.index(Rtl8188eusDkmsDriver) < drivers.index(RTL8188EUSDriver)
