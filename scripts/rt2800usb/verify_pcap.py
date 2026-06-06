"""Acceptance gate: replay-diff the rt2800usb (Ralink RT3572 / RT5372 / RT5572) firmware
upload against its cold-boot capture.

Drives the port's real ``RT2800USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so the chunked
``write_multi`` runs unchanged and the bundled rt2870.bin blob is byte-verified against the
wire.

Coverage grows with the port -- currently M2a: the rt2870.bin USB half (4096 bytes streamed
to FIRMWARE_IMAGE_BASE over 64-byte chunked control writes), anchored at the first chunk.

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
from wifit3.chips.rt2800usb.firmware import load_firmware_blob  # noqa: E402
from wifit3.chips.rt2800usb.transport import RT2800USBTransport  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rt2800usb"


def _is_fw_chunk_start(o: dict) -> bool:
    """The firmware upload streams to FIRMWARE_IMAGE_BASE (0x3000) in 64-byte chunks at
    incrementing addresses; anchor on the first chunk (the base address)."""
    return (o["dir"] == "OUT" and o["breq"] == USB_MULTI_WRITE
            and o["addr"] == FIRMWARE_IMAGE_BASE)


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    allops = rp.extract_ops(pcap, dev)
    csr0 = next((o for o in allops if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    silicon = (int.from_bytes(csr0["data"], "little") >> 16) & 0xFFFF if csr0 else 0
    fw = load_firmware_blob()
    print(f"{name}: card=dev{dev}, {len(allops)} vendor ops, silicon=0x{silicon:04x}, "
          f"fw={len(fw)}B")

    block = rp.extract_ops(pcap, dev, start=_is_fw_chunk_start)
    rd = rp.ReplayDevice(block)
    t = RT2800USBTransport(rd)
    try:
        t.write_multi(FIRMWARE_IMAGE_BASE, fw)   # 64-byte chunks, incrementing address
    except rp.Divergence as e:
        print(f"\nFAIL (divergence):\n  {e}")
        print(f"  reproduced {rd.i} firmware chunks before diverging")
        return 1

    print(f"\nPASS: rt2870.bin upload verified byte-for-byte -- {len(fw)} bytes over {rd.i} "
          f"chunked control writes from 0x{FIRMWARE_IMAGE_BASE:04x}. The bundled blob equals "
          f"the firmware the vendor driver streamed to the chip.")
    print("  NOTE: load_firmware's preamble write32(AUTOWAKEUP_CFG, 0) is absent from this "
          "USB capture (PCI/SoC-only in rt2800lib.c) -- a real divergence flagged for a "
          "separate session.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
