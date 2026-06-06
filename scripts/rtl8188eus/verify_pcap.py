"""Acceptance gate: verify the rtl8188eus (RTL8188EUS, TL-WN722N v2/v3) firmware upload
against its cold-boot capture.

The 8188e is rtl8xxxu, not rtw88, but the USB register wire format is identical (Realtek
vendor 0x05, address in wValue), so it reuses the shared Realtek replay engine.

Coverage grows with the port -- currently M1: the rtl8188eufw.bin payload as it lands on the
wire. The loader streams the payload to REG_FW_START_ADDRESS (0x1000) in 196-byte chunks
(``_writeN`` restarts at 0x1000 per 4096-byte page), so every write into [0x1000, 0x2000)
concatenated in order IS the uploaded firmware. This byte-verifies the bundled blob against
what the vendor driver actually pushed to the chip.

Not yet gated (flagged for a separate session): ``download_firmware``'s register *preamble*
is not wire-faithful -- it opens by re-enabling the 8051 (``SYS_FUNC+1 |= 4`` then
``SYS_FUNC |= CPU_ENABLE``), but the capture performs that enable at power-on, not at the
download, and its download region instead writes 0x0214/0x0200/0x010c/0x0116/0x0104 before
the MCU_FW_DL enable. So a whole-function replay diverges at op 1; the blob check below is
independent of that.

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

CAP_DIR = REPO / "usb_dumps_new" / "captures_8188eu"
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
    print("  NOTE: download_firmware's register preamble re-enables the 8051 via SYS_FUNC, "
          "which this capture does at power-on (not at the download) -- a wire-faithfulness "
          "divergence flagged for a separate session.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
