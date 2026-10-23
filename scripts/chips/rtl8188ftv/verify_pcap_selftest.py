"""Adversarial self-test of the RTL8188FTV verify_pcap gate.

A gate that PASSES a correct port is worthless if it also passes a BROKEN
one.  This mutates one op-class at a time (corrupt a return value / inject
a bogus write), re-runs verify_pcap against the SAME capture, and asserts
the gate flips to FAIL.  A mutation the gate still PASSES is a blind spot.

    uv run python scripts/chips/rtl8188ftv/verify_pcap_selftest.py
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "chip_verify_pcap", Path(__file__).resolve().parent / "verify_pcap.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)

from wifit3.chips.rtl8188ftv import chan, efuse, firmware, mac, phy, tx
from wifit3.chips.rtl8188ftv.constants import FW_HEADER_SIZE

FW_PAYLOAD_BYTE = FW_HEADER_SIZE  # first payload byte after the stripped header


class Mutation:
    """One monkeypatch that corrupts a single op-class of driver output."""

    def __init__(self, name, op_class, module, attr, wrap, capture):
        self.name, self.op_class = name, op_class
        self.module, self.attr, self.wrap, self.capture = module, attr, wrap, capture
        self.calls = 0

    def __enter__(self):
        self._orig = getattr(self.module, self.attr)
        counter = self

        def patched(*a, **k):
            counter.calls += 1
            return self.wrap(self._orig, *a, **k)
        setattr(self.module, self.attr, patched)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.attr, self._orig)


# --- corruption styles -------------------------------------------------------

def _corrupt_return_int(orig, *a, **k):
    return orig(*a, **k) ^ 0x1


def _corrupt_return_bytes(orig, *a, **k):
    b = bytearray(orig(*a, **k))
    b[0] ^= 0x80
    return bytes(b)


def _corrupt_fw_payload(orig, *a, **k):
    """Flip a byte in the FW *payload* (past the 32-byte header).  The blob
    gate strips the header before comparing, so corrupting the header would
    be invisible — corrupt where the comparison actually looks."""
    b = bytearray(orig(*a, **k))
    b[FW_PAYLOAD_BYTE] ^= 0x80
    return bytes(b)


def _prepend_bogus_write(orig, t, *a, **k):
    t.write32(0xFFF8, 0xDEADBEEF)
    return orig(t, *a, **k)


MUTATIONS = [
    # EFUSE read+parse (first gate — drives read_and_parse over the map region)
    Mutation("efuse.read_efuse_map (EFUSE map reads)", "bring-up",
             efuse, "read_efuse_map", _prepend_bogus_write, "capture-1"),
    # FW blob payload (the parameter's identity gate — blob must land byte-for-byte)
    Mutation("firmware.load_firmware_blob (FW payload)", "bring-up",
             firmware, "load_firmware_blob", _corrupt_fw_payload, "capture-1"),
    # FW download dance (pre-flight SYS_FUNC/MCU_FW_DL + page-select + csum — 189 ops)
    Mutation("firmware.download_firmware (FW download dance)", "bring-up",
             firmware, "download_firmware", _prepend_bogus_write, "capture-1"),
    # FW start dance (reset_8051 RSV_CTRL + SYS_FUNC + MCU poll + REG_HMTFR — 85 ops)
    Mutation("firmware.start_firmware (FW start dance)", "bring-up",
             firmware, "start_firmware", _prepend_bogus_write, "capture-1"),
    # post-FW antenna-selection init (PAD_CTRL1/GPIO_MUXCFG/LEDCFG0/RFE/PWR_DATA — 16 ops)
    Mutation("phy.init_antenna_selection (post-FW antenna sel)", "bring-up",
             phy, "init_antenna_selection", _prepend_bogus_write, "capture-1"),
    # MAC init table (101 ops — the anchor for the bring-up gate)
    Mutation("mac.apply_mac_init_table (MAC table writes)", "bring-up",
             mac, "apply_mac_init_table", _prepend_bogus_write, "capture-1"),
    # post_mac_init_phy (BB + AGC + crystal cap + RF preamble — 392 ops)
    Mutation("phy.post_mac_init_phy (BB+AGC+xtal+RF)", "bring-up",
             phy, "post_mac_init_phy", _prepend_bogus_write, "capture-1"),
    # LC calibration (LSTF/TXPAUSE branch + RF MODE_AG poll — 47 ops)
    Mutation("phy.lc_calibrate (LC calibration)", "bring-up",
             phy, "lc_calibrate", _prepend_bogus_write, "capture-1"),
    # IQ calibration (phy path A inner/outer + matrix — 378 ops)
    Mutation("phy.iq_calibrate (IQ calibration)", "bring-up",
             phy, "iq_calibrate", _prepend_bogus_write, "capture-1"),
    # enable_rf (EFUSE BB-gain trim + RF_CTRL + path A — 19 ops)
    Mutation("phy.enable_rf (RF enable + gain trim)", "bring-up",
             phy, "enable_rf", _prepend_bogus_write, "capture-1"),
    # enable_rx_path (filt maps + AGC IGI — 4 ops)
    Mutation("mac.enable_rx_path (RX path start)", "bring-up",
             mac, "enable_rx_path", _prepend_bogus_write, "capture-1"),
    # configure_filter (RCR monitor writes — 3 calls)
    Mutation("mac.configure_filter (RCR monitor filter)", "bring-up",
             mac, "configure_filter", _prepend_bogus_write, "capture-1"),
    # set_tx_power (per-channel TX power — 53 hops)
    Mutation("phy.set_tx_power (TX power per hop)", "bring-up",
             phy, "set_tx_power", _prepend_bogus_write, "capture-1"),
    # set_channel_2g_20mhz (spur cal + BB + RF — 54 calls)
    Mutation("chan.set_channel_2g_20mhz (channel tune)", "bring-up",
             chan, "set_channel_2g_20mhz", _prepend_bogus_write, "capture-1"),
    # TX wire shape is OUT of this capture's scope (0 bulk-OUT URBs in the
    # cold-boot session) — kept so a future capture with injects exercises it.
    Mutation("tx.build_tx_desc_mgmt (TX descriptor)", "inject",
             tx, "build_tx_desc_mgmt", _corrupt_return_bytes, "capture-1"),
]

# Op-classes the cold-boot capture cannot exercise by design (TX inject);
# they are covered by unit tests asserting exact wire shape (tests/chips/rtl8188ftv/test_tx.py).
OUT_OF_SCOPE = {"tx.build_tx_desc_mgmt (TX descriptor)"}


def _run_gate(capture: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return vp.run(capture)


def main() -> int:
    print("baseline (unmutated) — gate must PASS first:")
    rc = _run_gate("capture-1")
    print(f"  capture-1      {'PASS' if rc == 0 else f'FAIL(rc={rc})'}")
    if rc != 0:
        print("  !! baseline is not green — fix the gate before trusting the selftest")
        return 2

    print("\nmutation                                    class        calls  gate     verdict")
    print("-" * 92)
    blind = 0
    inconclusive = 0
    for m in MUTATIONS:
        with m:
            rc = _run_gate(m.capture)
        if m.calls == 0:
            if m.name in OUT_OF_SCOPE:
                verdict, tag = "OUT OF CAPTURE SCOPE (unit-tested)", "~"
            else:
                verdict, tag = "NOT EXERCISED (try another capture)", "?"
                inconclusive += 1
        elif rc != 0:
            verdict, tag = "caught", "OK"
        else:
            verdict, tag = "*** BLIND SPOT — gate PASSED a broken driver ***", "!!"
            blind += 1
        print(f"  {m.name:<42} {m.op_class:<11} {m.calls:>5}  "
              f"{'PASS' if rc == 0 else 'FAIL':<7} [{tag}] {verdict}")

    print("-" * 92)
    if blind:
        print(f"RESULT: {blind} BLIND SPOT(S) — the gate does not catch these divergences.")
        return 1
    if inconclusive:
        print(f"RESULT: no blind spots, but {inconclusive} class(es) not exercised by their capture "
              "— rerun those against a capture that exercises them before trusting coverage.")
        return 3
    print("RESULT: every exercised op-class flips the gate to FAIL — divergences DO show up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
