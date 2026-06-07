"""Byte-for-byte replay-diff of the rtl8188eus_dkms port against a vendor cold-boot capture.

PASS means ONLY: for this captured boot, the port emits the same USB bytes the vendor
driver did. It is a faithfulness gate, not a correctness proof (the only real proof is
beacons off the antenna). Fully offline -- no hardware.

Rides the shared rtw88-family replay engine (``scripts/rtw88_pcap_replay``): the 8188e uses
the same Realtek vendor request (bRequest 0x05), and its FW-page control writes map onto the
engine's ``write_block`` alias.

Milestones verified (built up as the port progresses):
  M1  power-on (CARDEMU_TO_ACT + REG_CR), verified from the first op
  M1  firmware download + FW-ready, verified from REG_MCUFWDL (start_addr=0x80)

The efuse probe read (region between power-on and FW) is verified separately once ported;
until then M1 verifies power-on and the FW download as two windows.

    uv run python scripts/rtl8188eus_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus_dkms import bb, efuse, firmware, mac, pwrseq, rf  # noqa: E402

REG_MCUFWDL = 0x0080
REG_SYS_CFG = 0x00F0
REG_APS_FSMCO_B2 = 0x0006   # first power-seq op (CARDEMU_TO_ACT step 1 poll)
DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8188eu" / "capture-1.pcap"


def _strip_async_watchdog(ops):
    """Remove the async 2 s-watchdog's per-tick sreset read (R REG_SYS_CFG/4),
    which a background kernel thread interleaves into the single EP0 stream every
    ~2.016 s (first fire ~frame 2731). The synchronous init/tune path never issues
    a 32-bit REG_SYS_CFG read (read_chip_version runs once at probe, before this
    window), so this is unambiguous. The watchdog's *DIG* burst (FA counters, NHM,
    EDCCA) is a separate runtime concern verified by the DIG-watchdog milestone."""
    return [o for o in ops
            if not (o["kind"] == "R" and o.get("addr") == REG_SYS_CFG and o["width"] == 4)]


def _verify_pre_fw(pcap, dev):
    """The probe-phase chain from the first power-seq op (the 0x06 power-ready poll),
    contiguous on one transport (this region is before the watchdog's first fire):
        power-on    (CARDEMU_TO_ACT + REG_CR)
        efuse read  (IOL READ_EFUSE_MAP + packet-buffer readback + PG decode)
    Returns (milestones, ChipParams) -- the decoded crystal_cap feeds the main chain,
    closing the M2b hardcode. (The [0..5] chip-version prologue ahead of 0x06 and the
    MISC01 queue/page setup after the efuse read are not yet ported -- small gaps.)"""
    ops = rp.extract_ops(pcap, dev, start_addr=REG_APS_FSMCO_B2)
    t = rp.ReplayTransport(ops)
    miles = []
    pwrseq.power_on(t)
    miles.append(("power-on", t.i))
    params = efuse.read_chip_params(t)
    miles.append(("efuse", t.i))
    return miles, params


def _verify_main_chain(pcap, dev, params):
    """The contiguous post-power-on bring-up, verified from the first REG_MCUFWDL op.
    Each milestone consumes the next span of the same transport byte-for-byte:
        M1   firmware download + FW-ready + InitializeFirmwareVars
        M2a  PHY_MACConfig8188E (MAC reg table + MAX_AGGR_NUM)
        M2b  PHY_BBConfig8188E (BB enable + PHY_REG + AGC_TAB + crystal cap)
        M2c  PHY_RFConfig8188E (RFENV setup + radio_a table + restore)
    The decoded crystal_cap from the probe efuse read feeds M2b (no hardcode)."""
    ops = _strip_async_watchdog(rp.extract_ops(pcap, dev, start_addr=REG_MCUFWDL))
    t = rp.ReplayTransport(ops)
    miles = []
    firmware.download_firmware(t, firmware.load_firmware_blob())
    miles.append(("M1 fw", t.i))
    mac.phy_mac_config(t)
    miles.append(("M2a mac", t.i))
    bb.phy_bb_config(t, crystal_cap=params.crystal_cap)
    miles.append(("M2b bb", t.i))
    rf.phy_rf_config(t)
    miles.append(("M2c rf", t.i))
    efuse.iol_efuse_patch(t)
    miles.append(("M2d efpatch", t.i))
    mac.init_tx_buffer_boundary(t)
    mac.init_llt(t)
    miles.append(("M2e llt", t.i))
    return miles


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    ops = rp.extract_ops(pcap, dev)
    print(f"{pcap.name}: card=dev{dev}, {len(ops)} vendor ops")

    try:
        pre_miles, params = _verify_pre_fw(pcap, dev)
    except rp.Divergence as e:
        print(f"\nFAIL pre-FW @ first divergence:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug): {type(e).__name__}: {e}")
        return 2

    prev = 0
    for label, end in pre_miles:
        print(f"  PASS {label:10} {end - prev:5} ops")
        prev = end
    mac = params.mac_address
    mac_show = (f"{mac[0]:02x}:{mac[1]:02x}:{mac[2]:02x}:xx:xx:xx (OUI)"
                if len(mac) == 6 else "invalid")
    print(f"       efuse: crystal_cap=0x{params.crystal_cap:02x}, mac={mac_show}")

    try:
        miles = _verify_main_chain(pcap, dev, params)
    except rp.Divergence as e:
        print(f"\nFAIL @ first divergence:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug): {type(e).__name__}: {e}")
        return 2

    prev = 0
    for label, end in miles:
        print(f"  PASS {label:10} {end - prev:5} ops")
        prev = end
    print("\nPASS: power-on + efuse + firmware + MAC/BB/RF/efuse-patch/LLT byte-for-byte.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
