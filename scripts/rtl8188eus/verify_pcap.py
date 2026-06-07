"""Acceptance gate: verify the rtl8188eus (RTL8188EUS, TL-WN722N v2/v3) firmware upload
against its cold-boot capture.

The 8188e is rtl8xxxu, not rtw88, but the USB register wire format is identical (Realtek
vendor 0x05, address in wValue), so it reuses the shared Realtek replay engine.

Coverage grows with the port -- currently M1: the rtl8188eufw.bin payload as it lands on the
wire. The loader streams the payload to REG_FW_START_ADDRESS (0x1000) in 196-byte chunks
(``_writeN`` restarts at 0x1000 per 4096-byte page), so every write into [0x1000, 0x2000)
concatenated in order IS the uploaded firmware. This byte-verifies the bundled blob against
what the vendor driver actually pushed to the chip.

This capture set is the **mainline rtl8xxxu** cold boot (``airmon-ng.log``: driver rtl8xxxu) --
the driver this port mirrors -- so the register *sequence* replays byte-for-byte, not just the
blob. (The DKMS/vendor ``realtek-rtl8188eus`` boot in ``usb_dumps_new/captures_8188eu/`` is the
target of a separate vendor port.) Coverage grows milestone-by-milestone from M1 (blob) outward.

Run: uv run python scripts/rtl8188eus/verify_pcap.py [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus import firmware  # noqa: E402
from wifit3.chips.rtl8188eus.constants import FW_HEADER_SIZE, REG_FW_START_ADDRESS  # noqa: E402

CAP_DIR = REPO / "usb_dumps" / "captures_rtl8xxxu"
_WHOLE = (1, 10 ** 9)
_FW_REGION_END = REG_FW_START_ADDRESS + 0x1000   # chunks restart at 0x1000 each page


def run(cap: str | None = None) -> int:
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    payload = firmware.load_firmware_blob()[FW_HEADER_SIZE:]
    ops = rp.extract_ops(pcap, dev, _WHOLE)
    chunks = [o for o in ops
              if o["kind"] == "W" and REG_FW_START_ADDRESS <= o["addr"] < _FW_REGION_END]
    uploaded = b"".join(o["value"].to_bytes(o["width"], "little") for o in chunks)
    print(f"{name}: card=dev{dev}, blob payload {len(payload)}B, captured upload "
          f"{len(uploaded)}B over {len(chunks)} chunks")

    if uploaded != payload:
        n = min(len(uploaded), len(payload))
        j = next((k for k in range(n) if uploaded[k] != payload[k]), n)
        print(f"\nFAIL: uploaded firmware differs from the bundled rtl8188eufw.bin at byte "
              f"{j} (upload {len(uploaded)}B vs blob {len(payload)}B)")
        return 1

    print(f"\nPASS: rtl8188eufw.bin upload verified byte-for-byte -- {len(payload)}B payload "
          f"streamed to REG_FW_START_ADDRESS matches the bundled blob.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
