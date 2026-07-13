"""Tests for the runtime WPS default-PIN generators + gated dispatch (wps_algos).

Vectors are for the synthetic BSSID 00:11:22:33:44:55, cross-checked against the published
algorithm descriptions (3WiFi / devttys0 / WPS-OUI-PINS plan §2).
"""

import subprocess
import sys

from wifit3.engine.attacks.wps import wps_algos as A
from wifit3.engine.attacks.wps import wps_router_ouis
from wifit3.engine.attacks.wps.wsc_crypto import pin_is_valid

_MAC = bytes.fromhex("001122334455")
_OUI_MOD = "wifit3.engine.attacks.wps.wps_router_ouis"


def test_generator_vectors():
    assert A.pin24(_MAC) == ["33598291"]
    assert A.pin_airocon(_MAC) == ["71593579"]
    assert A.pin_dlink(_MAC) == ["67456000"]
    assert A.pin_dlink1(_MAC) == ["56271874"]
    assert A.pin_asus(_MAC) == ["10403853"]


def test_every_emitted_pin_is_checksum_valid():
    # Edge MACs exercise the ASUS variable modulus and the D-Link 7-digit forcing.
    for mac in (_MAC, bytes.fromhex("ffffff000000"), bytes.fromhex("fedcba987654"),
                bytes.fromhex("000000000000"), bytes.fromhex("ffffffffffff")):
        cands = A.pins_for(mac)
        assert cands, "generators must always produce candidates"
        assert all(len(p) == 8 and p.isdigit() and pin_is_valid(p) for p in cands)
        assert len(cands) == len(set(cands))          # order-preserving dedup


def test_gate_not_flood_unknown_oui():
    # An OUI not in the table gets ONLY the broad chipset algorithms — no brand flood.
    unknown = bytes.fromhex("fedcba987654")
    assert unknown[:3].hex().upper() not in wps_router_ouis.OUI_VENDOR
    assert A.pins_for(unknown) == list(dict.fromkeys(A.pin24(unknown) + A.pin_airocon(unknown)))


def test_dlink_oui_gates_dlink_generators_first():
    dlink_oui = next(o for o, v in wps_router_ouis.OUI_VENDOR.items() if v == "dlink")
    mac = bytes.fromhex(dlink_oui + "010203")
    got = A.pins_for(mac)
    assert got[0] == A.pin_dlink(mac)[0]                         # brand-specific first
    assert set(A.pin_dlink(mac) + A.pin_dlink1(mac)).issubset(got)
    assert A.pin24(mac)[0] in got                                # broad still present


def test_asus_oui_gates_asus_generator():
    asus_oui = next(o for o, v in wps_router_ouis.OUI_VENDOR.items() if v == "asus")
    mac = bytes.fromhex(asus_oui + "010203")
    assert A.pin_asus(mac)[0] in A.pins_for(mac)


def test_oui_table_has_expected_families():
    vendors = set(wps_router_ouis.OUI_VENDOR.values())
    assert vendors == {"dlink", "asus", "belkin", "thomson", "edimax", "upvel", "huawei"}
    assert len(wps_router_ouis.OUI_VENDOR) > 300           # ~2359 from IEEE, sanity floor


def test_oui_table_is_lazy_loaded():
    # The ~7 KB table must NOT import at module load (protects app startup time) — only
    # when pins_for actually runs. Checked in a fresh interpreter to avoid cross-test state.
    code = (
        "import sys, wifit3.engine.attacks.wps.wps_algos as a; "
        f"assert {_OUI_MOD!r} not in sys.modules, 'table imported at module load'; "
        "a.pins_for(bytes.fromhex('001122334455')); "
        f"assert {_OUI_MOD!r} in sys.modules, 'table not imported after pins_for'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_vendor_statics_are_checksum_valid():
    for vendor_pins in A._VENDOR_STATICS.values():
        for p in vendor_pins:
            assert len(p) == 8 and p.isdigit() and pin_is_valid(p), p


def test_brand_static_pin_seeded_for_matching_oui():
    thomson_oui = next(o for o, v in wps_router_ouis.OUI_VENDOR.items() if v == "thomson")
    mac = bytes.fromhex(thomson_oui + "010203")
    assert "67958146" in A.pins_for(mac)                    # Thomson's fixed default PIN
