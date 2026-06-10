"""Acceptance gate: replay-diff rt2800usb (Ralink RT3572 / RT5372 / RT5572) bring-up blocks
against its cold-boot capture.

Drives the port's real ``RT2800USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so the port's real helpers
run unchanged and must emit byte-identical writes or a Divergence is raised at the first
mismatch.

This is an *anchored-block* verifier (not the single-cursor whole-capture walk the clean-room
ports use): each block is extracted from the capture by an anchor predicate and replayed in
isolation, so coverage grows block by block without re-porting the whole driver. Blocks:

  * [EFUSE walk]      the 32-iteration EFUSE read loop (``read_eeprom_efuse``), anchored on
                      the first EFUSE_CTRL touch. Catches the ADDRESS_IN byte-vs-word bug.
  * [firmware upload] the rt2870.bin USB half (4096 bytes streamed to FIRMWARE_IMAGE_BASE
                      over 64-byte chunked control writes), anchored at the first chunk.

Known divergence flagged for a separate session: the full ``load_firmware`` preamble opens
with ``write32(AUTOWAKEUP_CFG, 0)``, which this USB capture never issues -- that write is
PCI/SoC-only in rt2800lib.c, so the USB port should skip it. The blob upload below is
verified independently of that preamble.

Run: uv run python scripts/rt2800usb/verify_pcap.py [capture-1|capture-2]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rt2x00_pcap_replay as rp  # noqa: E402
from wifit3.chips.rt2800usb.constants import (  # noqa: E402
    FIRMWARE_IMAGE_BASE,
    MAC_CSR0,
    USB_MULTI_WRITE,
)
from wifit3.chips.rt2800usb.eeprom import (  # noqa: E402
    EFUSE_CTRL,
    parse_eeprom,
    read_eeprom_efuse,
)
from wifit3.chips.rt2800usb.firmware import load_firmware_blob  # noqa: E402
from wifit3.chips.rt2800usb.transport import RT2800USBTransport  # noqa: E402

# Search the RT5372 (PAU05) capture first, then the RT5572 (PAU09) one.
CAP_DIRS = [
    REPO / "usb_dumps" / "captures_rt2800usb_rt5372",   # RT5372 / PAU05-class
    REPO / "usb_dumps_new" / "captures_rt2800usb",       # RT5572 / PAU09-class
]


def _is_efuse_ctrl(o: dict) -> bool:
    """First touch of EFUSE_CTRL == rt2800_efuse_detect's PRESENT read, which opens the
    EFUSE read loop; anchor the block there so read_eeprom_efuse replays op-for-op."""
    return o["addr"] == EFUSE_CTRL


def _is_fw_chunk_start(o: dict) -> bool:
    """The firmware upload streams to FIRMWARE_IMAGE_BASE (0x3000) in 64-byte chunks at
    incrementing addresses; anchor on the first chunk (the base address)."""
    return (o["dir"] == "OUT" and o["breq"] == USB_MULTI_WRITE
            and o["addr"] == FIRMWARE_IMAGE_BASE)


def verify_efuse(pcap: Path, dev: int) -> bool:
    """Anchored block: replay the port's real ``read_eeprom_efuse`` against the capture's
    EFUSE read loop. The shipping reader writes ``EFUSE_CTRL.ADDRESS_IN`` as a BYTE offset,
    but the chip (and the kernel, ``rt2800lib.c`` ``i += 8`` words) treat it as a u16-WORD
    index -- so the buggy reader diverges on the 2nd iteration's KICK write. A word-offset
    reader (``ADDRESS_IN = offset // 2``) reproduces the whole loop byte-for-byte."""
    print("\n[EFUSE walk]")
    block = rp.extract_ops(pcap, dev, start=_is_efuse_ctrl)
    rd = rp.ReplayDevice(block)
    t = RT2800USBTransport(rd)
    try:
        eeprom = read_eeprom_efuse(t)
    except rp.Divergence as e:
        print(f"  FAIL (divergence): {e}")
        print(f"  reproduced {rd.i} EFUSE ops before diverging "
              f"-- the ADDRESS_IN byte-vs-word bug (see RT2800USB.md).")
        return False
    ev = parse_eeprom(eeprom)
    print(f"  PASS: EFUSE read loop reproduced byte-for-byte over {rd.i} ctrl ops.")
    print(f"  capture-unit decode: NIC_CONF0=0x{ev.nic_conf0:04x} "
          f"(rxpath={ev.rxpath} txpath={ev.txpath}), freq_offset={ev.freq_offset}, "
          f"lna_gain_bg=0x{ev.lna_gain_bg:02x}, rssi_bg0=0x{ev.rssi_bg_offset0:02x}")
    return True


def verify_fw(pcap: Path, dev: int, fw: bytes) -> bool:
    """Anchored block: stream the bundled rt2870.bin and byte-verify it against the wire."""
    print("\n[firmware upload]")
    block = rp.extract_ops(pcap, dev, start=_is_fw_chunk_start)
    rd = rp.ReplayDevice(block)
    t = RT2800USBTransport(rd)
    try:
        t.write_multi(FIRMWARE_IMAGE_BASE, fw)   # 64-byte chunks, incrementing address
    except rp.Divergence as e:
        print(f"  FAIL (divergence): {e}")
        print(f"  reproduced {rd.i} firmware chunks before diverging")
        return False
    print(f"  PASS: rt2870.bin upload verified byte-for-byte -- {len(fw)} bytes over {rd.i} "
          f"chunked control writes from 0x{FIRMWARE_IMAGE_BASE:04x}.")
    print("  NOTE: load_firmware's preamble write32(AUTOWAKEUP_CFG, 0) is absent from this "
          "USB capture (PCI/SoC-only in rt2800lib.c) -- a real divergence flagged separately.")
    return True


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    arg = cap or "capture-1"
    if arg.endswith(".pcap") or "/" in arg or "\\" in arg:   # explicit path (rel to REPO ok)
        pcap = Path(arg) if Path(arg).is_absolute() else REPO / arg
    else:                                                    # bare name: search known dirs
        pcap = next((d / f"{arg}.pcap" for d in CAP_DIRS if (d / f"{arg}.pcap").exists()), None)
    if pcap is None or not pcap.exists():
        print(f"FAIL: cannot find capture for '{arg}'")
        return 1
    name = pcap.stem
    print(f"using {pcap}")

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    allops = rp.extract_ops(pcap, dev)
    csr0 = next((o for o in allops if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    silicon = (int.from_bytes(csr0["data"], "little") >> 16) & 0xFFFF if csr0 else 0
    fw = load_firmware_blob()
    print(f"{name}: card=dev{dev}, {len(allops)} vendor ops, silicon=0x{silicon:04x}, "
          f"fw={len(fw)}B")

    efuse_ok = verify_efuse(pcap, dev)
    fw_ok = verify_fw(pcap, dev, fw)
    return 0 if (efuse_ok and fw_ok) else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
