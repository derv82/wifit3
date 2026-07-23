"""Tests for the Focus live-packet-dashboard counters (pure PacketStats tally). The RX/TX wiring
now lives in WlanSink (see test_sink.py: update() records RX, record_tx() classifies TX)."""

from wifit3.wlan.packet_stats import PACKET_CLASSES, PacketStats

BSSID = "00:11:22:33:44:55"


def test_rx_class_mapping():
    s = PacketStats()
    s.record_rx(BSSID, "beacon")
    s.record_rx(BSSID, "data")
    s.record_rx(BSSID, "qos_data")   # folds into 'data'
    s.record_rx(BSSID, "wep_data")   # → wep_iv
    s.record_rx(BSSID, "eapol")
    s.record_rx(BSSID, "deauth")
    snap = s.snapshot(BSSID)
    assert snap["beacon"] == 1
    assert snap["data"] == 2
    assert snap["wep_iv"] == 1
    assert snap["eapol"] == 1
    assert snap["deauth"] == 1
    assert snap["inject"] == 0


def test_rx_untracked_types_are_noop():
    s = PacketStats()
    for t in ("probe_req", "probe_resp", "assoc_req", "assoc_resp", "mgmt_5", "ctrl_11"):
        s.record_rx(BSSID, t)
    assert s.snapshot(BSSID) == dict.fromkeys(PACKET_CLASSES, 0)


def test_tx_deauth_vs_inject():
    s = PacketStats()
    s.record_tx(BSSID, is_deauth=True)
    s.record_tx(BSSID, is_deauth=False)
    s.record_tx(BSSID, is_deauth=False)
    snap = s.snapshot(BSSID)
    assert snap["deauth"] == 1
    assert snap["inject"] == 2


def test_snapshot_unknown_bssid_is_zero_filled_and_not_registered():
    s = PacketStats()
    snap = s.snapshot("de:ad:be:ef:00:00")
    assert snap == dict.fromkeys(PACKET_CLASSES, 0)
    # Reading must not create a registry entry (snapshot is side-effect free).
    assert "de:ad:be:ef:00:00" not in s._counts


def test_snapshot_returns_a_copy():
    s = PacketStats()
    s.record_rx(BSSID, "beacon")
    snap = s.snapshot(BSSID)
    snap["beacon"] = 999
    assert s.snapshot(BSSID)["beacon"] == 1
