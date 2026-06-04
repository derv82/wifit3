"""Extract the 8821a NIC firmware blob from the vendor C array.

`array_mp_8821a_fw_nic[]` in `hal/rtl8812a/hal8821a_fw.c` is the NIC (no-BT) image
the cold-boot driver downloads. Writes `assets/rtl8821au_fw.bin` (32-byte header
included; the download path strips it). The body bytes are verified against the
cold-boot wire by `verify_pcap.py`.

Run: uv run python scripts/rtl8821au_dkms/extract_fw.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "usb_dumps_new/captures_rtl8821au/driver-source/hal/rtl8812a/hal8821a_fw.c"
OUT = REPO / "src/wifit3/chips/rtl8821au_dkms/assets/rtl8821au_fw.bin"


def main() -> int:
    text = SRC.read_text(errors="replace")
    # Match the NIC array specifically (the [] guards against _nic_bt[]).
    m = re.search(r"array_mp_8821a_fw_nic\[\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        print("FAIL: array_mp_8821a_fw_nic[] not found in", SRC)
        return 1
    nums = re.findall(r"0x([0-9A-Fa-f]{2})", m.group(1))
    blob = bytes(int(b, 16) for b in nums)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(blob)
    print(f"wrote {OUT.relative_to(REPO)}")
    print(f"  {len(blob)} bytes (32 header + {len(blob) - 32} body)")
    print(f"  sha256: {hashlib.sha256(blob).hexdigest()}")
    print(f"  first body byte (after 32B hdr): 0x{blob[32]:02x} (expect 0x02 = 8051 LJMP)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
