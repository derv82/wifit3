"""Hardware-free regression for the no-BT (bt_coexist=FALSE) WiFi-only coex path (issue #53).

The pcap card is a combo (bt_coexist=TRUE), so the byte gate never exercises these branches; this
pins the register writes the wifi-only front-end config / antenna switch emit and confirms the
no-BT set_channel band-switch routes through btcwifionly without touching t.btc.
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821cu_dkms import btcwifionly, chan


class Rec:
    """Records ops and serves canned reads; accessing .btc fails (no-BT card has none)."""

    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}
        self.rega24 = self.rega28 = self.regaac = 0
        self.current_band = -1
        self.current_channel = 0
        self.thermal_reset_pending = False

    @property
    def btc(self):
        raise AssertionError("no-BT path must not touch t.btc")

    def read8(self, a):
        self.ops.append(("R8", a))
        return self.reads.get(a, 0)

    def read16(self, a):
        self.ops.append(("R16", a))
        return self.reads.get(a, 0)

    def read32(self, a):
        self.ops.append(("R32", a))
        return self.reads.get(a, 0)

    def write8(self, a, v):
        self.ops.append(("W8", a, v))

    def write16(self, a, v):
        self.ops.append(("W16", a, v))

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def _w32(rec):
    return [o for o in rec.ops if o[0] == "W32"]


def _info(rfe=0x22, bt_coexist=False):
    return SimpleNamespace(rfe_type=rfe, bt_coexist=bt_coexist, chip_ver=4)


def test_hw_config_reference_emits_gnt_and_coex_tables():
    rec = Rec()
    btcwifionly.hw_config(rec, _info())
    assert _w32(rec) == [
        ("W32", 0x70, 0x04000000),      # gnt owner -> WL
        ("W32", 0x1704, 0x7700),        # gnt_wl=1, gnt_bt=0 wdata
        ("W32", 0x1700, 0xC00F0038),    # LTE-coex indirect write 0x38
        ("W32", 0x06C0, 0xAAAAAAAA),
        ("W32", 0x06C4, 0xAAAAAAAA),
    ]


def test_switch_antenna_2g_reference_wlg_at_btg():
    # rfe 0x22 -> module type 2: WLG at BTG, main port -> no polarity invert -> regval 0x1.
    rec = Rec()
    btcwifionly.switch_antenna(rec, _info(), is_5g=False)
    assert _w32(rec) == [
        ("W32", 0x4C, 0x01000000),
        ("W32", 0xCB4, 0x77),
        ("W32", 0xCB4, 0x10000000),
    ]


def test_switch_antenna_5g_reference():
    # 5G pos (TO_WLA) never inverts polarity -> regval 0x1, same DPDT writes.
    rec = Rec()
    btcwifionly.switch_antenna(rec, _info(), is_5g=True)
    assert _w32(rec) == [
        ("W32", 0x4C, 0x01000000),
        ("W32", 0xCB4, 0x77),
        ("W32", 0xCB4, 0x10000000),
    ]


def test_switch_antenna_2g_wlg_at_wlag_inverts_polarity():
    # rfe 0 -> module type 0: WLG at WLAG -> the WLG position inverts -> regval 0x2.
    rec = Rec()
    btcwifionly.switch_antenna(rec, _info(rfe=0), is_5g=False)
    assert ("W32", 0xCB4, 0x20000000) in _w32(rec)


def test_switch_antenna_no_ext_switch_is_silent():
    # rfe 5 -> module type 5: no external antenna switch -> early return, no writes.
    rec = Rec()
    btcwifionly.switch_antenna(rec, _info(rfe=5), is_5g=False)
    assert rec.ops == []


def test_set_channel_no_bt_routes_wifionly_without_touching_btc(monkeypatch):
    # Isolate the coex branch: stub the band/channel/bandwidth/tx-power sub-steps.
    for name in ("_switch_band", "_set_bb_swing_by_band_2g", "_switch_channel",
                 "_config_kfree", "_mac_switch_bandwidth", "_switch_bandwidth_20"):
        monkeypatch.setattr(chan, name, lambda *a, **k: None)
    monkeypatch.setattr(chan.txpower, "set_tx_power_level", lambda *a, **k: None)
    rec = Rec()
    chan.set_channel(rec, _info(), 1)                 # bt_coexist=False; must not raise (t.btc)
    # the wifi-only antenna switch ran (its 0x4c DPDT-SW write is the signature)
    assert ("W32", 0x4C, 0x01000000) in _w32(rec)
