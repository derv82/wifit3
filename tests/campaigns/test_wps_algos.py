"""Tests for the prioritized WPS default-PIN engine (wps_algos)."""

import subprocess
import sys

from wifit3.campaigns.wps import wps_algos as A
from wifit3.dot11.wsc.crypto import pin_is_valid

_MAC = bytes.fromhex("001122334455")
_PINDB_MOD = "wifit3.campaigns.wps.wps_pindb"


def test_generator_vectors():
    assert A.pin24(_MAC) == ["33598291"]
    assert A.pin_airocon(_MAC) == ["71593579"]
    assert A.pin_dlink(_MAC) == ["67456000"]
    assert A.pin_dlink1(_MAC) == ["56271874"]
    assert A.pin_asus(_MAC) == ["10403853"]
    assert A.pin_computepin_28(_MAC) == ["69142611"]
    assert A.pin_computepin_32(_MAC) == ["37851736"]
    assert A.pin_invnic(_MAC) == ["34173862"]
    assert A.pin_trendnet(_MAC) == ["05168859"]


def test_every_generator_pin_is_checksum_valid():
    for mac in (_MAC, bytes.fromhex("ffffff000000"), bytes.fromhex("fedcba987654"),
                bytes.fromhex("000000000000"), bytes.fromhex("ffffffffffff")):
        for algo in (A.pin24, A.pin_airocon, A.pin_dlink, A.pin_dlink1, A.pin_asus,
                     A.pin_computepin_28, A.pin_computepin_32, A.pin_invnic, A.pin_trendnet):
            pins = algo(mac)
            assert all(len(p) == 8 and p.isdigit() and pin_is_valid(p) for p in pins)


def test_pins_for_produces_valid_8digit_candidates():
    for mac in (_MAC, bytes.fromhex("ffffff000000"), bytes.fromhex("fedcba987654"),
                bytes.fromhex("000000000000"), bytes.fromhex("ffffffffffff")):
        cands = A.pins_for(mac)
        assert cands, "pins_for must always produce candidates"
        assert all(len(p) == 8 and p.isdigit() for p in cands)
        assert len(cands) == len(set(cands)), "candidates must be deduplicated"


def test_gate_not_flood_unknown_oui():
    unknown = bytes.fromhex("fedcba987654")
    got = A.pins_for(unknown)
    broad = list(dict.fromkeys(A.pin24(unknown) + A.pin_airocon(unknown)))
    assert got == broad


def test_model_pins_prioritized_first():
    mac = bytes.fromhex("001122334455")
    got = A.pins_for(mac, model="DIR-615")
    assert got[0] == "12345670"
    assert got[1] == "68175542"


def test_oui_exact_match_seeds_known_pins():
    # 000138 has factory PIN 35606543
    mac = bytes.fromhex("000138010203")
    got = A.pins_for(mac)
    assert "35606543" in got
    assert got.index("35606543") < got.index(A.pin24(mac)[0])


def test_dlink_vendor_gates_dlink_generators():
    mac = bytes.fromhex("001122334455")
    got = A.pins_for(mac, vendor="D-Link")
    assert A.pin_dlink(mac)[0] in got
    assert A.pin_dlink1(mac)[0] in got
    assert got.index(A.pin_dlink(mac)[0]) < got.index(A.pin24(mac)[0])


def test_asus_vendor_gates_asus_generator():
    mac = bytes.fromhex("001122334455")
    got = A.pins_for(mac, vendor="ASUSTek")
    assert A.pin_asus(mac)[0] in got
    assert got.index(A.pin_asus(mac)[0]) < got.index(A.pin24(mac)[0])


def test_ssid_pattern_matches_wlan_prefix():
    mac = bytes.fromhex("001122334455")
    got = A.pins_for(mac, ssid="WLAN_1234")
    assert "12345670" in got
    assert "11866428" in got


def test_string_bssid_normalized():
    assert A.pins_for("00:11:22:33:44:55") == A.pins_for(_MAC)
    assert A.pins_for("001122334455") == A.pins_for(_MAC)
    assert A.pins_for("invalid") == []


def test_pindb_is_lazy_loaded():
    code = (
        "import sys, wifit3.campaigns.wps.wps_algos as a; "
        f"assert {_PINDB_MOD!r} not in sys.modules, 'database imported at module load'; "
        "a.pins_for(bytes.fromhex('001122334455')); "
        f"assert {_PINDB_MOD!r} in sys.modules, 'database not imported after pins_for'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_historical_vendor_statics_preserved():
    mac = bytes.fromhex("001122334455")

    thomson_pins = A.pins_for(mac, vendor="Thomson Telecom")
    assert "67958146" in thomson_pins

    edimax_pins = A.pins_for(mac, vendor="Edimax Technology")
    assert "35611530" in edimax_pins

    upvel_pins = A.pins_for(mac, vendor="Upvel")
    for upvel_pin in ("20854836", "43977680", "05294176"):
        assert upvel_pin in upvel_pins

    dlink_pins = A.pins_for(mac, vendor="D-Link")
    assert "68175542" in dlink_pins


def test_raw_non_checksum_factory_pins_preserved():
    mac = bytes.fromhex("001122334455")
    pins = A.pins_for(mac)
    assert "12345678" in pins
    assert not pin_is_valid("12345678")
