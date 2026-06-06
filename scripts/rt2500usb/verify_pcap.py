"""Acceptance gate: replay-diff the rt2500usb (Ralink RT2570) bring-up against its
cold-boot capture.

Drives the port's real ``RT2500USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so every transport
helper -- regbusy_read, set_state's MAC_CSR17 poll, write16_mask, the single-writes --
runs unchanged and the port must emit byte-identical writes or a Divergence is raised.

RT2570 needs no firmware; bring-up is pure 16-bit CSR access (address in wIndex, vendor
bRequest 6/7). Coverage grows with the port -- currently the ``init_registers`` linear CSR
bring-up (rt2500usb.c:766-879), anchored at the USB_DEVICE_MODE test write that opens it.

Run: uv run python scripts/rt2500usb/verify_pcap.py [capture-1|capture-2]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rt2x00_pcap_replay as rp  # noqa: E402
from wifit3.chips.rt2500usb import bbp, chan, mac  # noqa: E402
from wifit3.chips.rt2500usb.constants import (  # noqa: E402
    EEPROM_ANTENNA,
    EEPROM_ANTENNA_RF_TYPE,
    MAC_CSR0,
    USB_DEVICE_MODE,
    USB_EEPROM_READ,
    USB_MODE_TEST,
)
from wifit3.chips.rt2500usb.transport import RT2500USBTransport, get_field16  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rt2500usb"


def _is_init_start(o: dict) -> bool:
    """init_registers opens with vendor_request_sw(USB_DEVICE_MODE, 0x0001, USB_MODE_TEST)
    -- an OUT single op with the test-mode value in wValue (rt2500usb.c:770)."""
    return (o["dir"] == "OUT" and o["breq"] == USB_DEVICE_MODE
            and o["wval"] == USB_MODE_TEST and o["addr"] == 0x0001)


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-2").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    allops = rp.extract_ops(pcap, dev)

    # MAC_CSR0 read recovers the chip revision; its low nibble selects the LNA branch.
    rev_op = next((o for o in allops if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    if rev_op is None:
        print("FAIL: no MAC_CSR0 (revision) read in capture")
        return 1
    revision = int.from_bytes(rev_op["data"], "little")

    # One-shot EEPROM read seeds the BBP overrides + the antenna/RF-type selection.
    ee = next((o for o in allops if o["dir"] == "IN" and o["breq"] == USB_EEPROM_READ), None)
    if ee is None:
        print("FAIL: no EEPROM read in capture")
        return 1
    eeprom = ee["data"]
    antenna_word = eeprom[EEPROM_ANTENNA * 2] | (eeprom[EEPROM_ANTENNA * 2 + 1] << 8)
    rf_type = get_field16(antenna_word, EEPROM_ANTENNA_RF_TYPE)
    ant_tx, ant_rx = chan.antenna_defaults(antenna_word)
    print(f"{name}: card=dev{dev}, {len(allops)} control ops, MAC_CSR0=0x{revision:04x}, "
          f"rf_type=0x{rf_type:x}, ant tx={ant_tx} rx={ant_rx}")

    # Contiguous bring-up block, anchored at the USB_DEVICE_MODE test write. init_registers
    # and init_bbp run back-to-back on the wire; the kernel then touches MAC_CSR20 (LED/RF)
    # before the antenna + RF tune, so config_ant / config_channel are non-contiguous here
    # and land as their own anchored blocks (future milestones).
    block = rp.extract_ops(pcap, dev, start=_is_init_start)
    rd = rp.ReplayDevice(block)
    t = RT2500USBTransport(rd)
    miles: list[tuple[str, int]] = []
    try:
        mac.init_registers(t, revision)                  # linear CSR bring-up (766-879)
        miles.append(("init_registers", rd.i))
        bbp.init_bbp(t, eeprom)                           # 31 BBP defaults + EEPROM (898-951)
        miles.append(("init_bbp", rd.i))
    except rp.Divergence as e:
        last = miles[-1][0] if miles else "(none)"
        print(f"\nFAIL (divergence after {last}):\n  {e}")
        _report(miles)
        return 1

    print(f"\nPASS: reproduced {rd.i} ops byte-for-byte -- init_registers + init_bbp "
          f"(RT2570 CSR + baseband bring-up). config_ant / config_channel are non-contiguous "
          f"(kernel interleaves MAC_CSR20) and not yet wired.")
    _report(miles)
    return 0


def _report(miles: list[tuple[str, int]]) -> None:
    prev = 0
    for label, end in miles:
        print(f"      {label:16} {end - prev:4} ops")
        prev = end


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
