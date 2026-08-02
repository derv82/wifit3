"""Extract the RTL8188EUS T-cut NIC firmware blob from the vendor source array.

The vendor driver ships the FW as a C byte array ``array_mp_8188e_t_fw_nic[]`` in
``hal/rtl8188e/hal8188e_t_fw.c`` (the FW_SOURCE_HEADER_FILE path -- no /lib/firmware
file is read on this card). This pulls those bytes verbatim into a .bin so the port
uploads the exact image the vendor driver did. ``verify_pcap.py`` then confirms it
matches the cold-boot wire byte-for-byte.

    uv run python scripts/rtl8188eus_dkms/extract_fw.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC_C = (REPO / "driver_captures" / "captures_8188eu" / "driver-source"
         / "hal" / "rtl8188e" / "hal8188e_t_fw.c")
OUT = REPO / "src" / "wifit3" / "chips" / "rtl8188eus_dkms" / "assets" / "rtl8188eufw.bin"
ARRAY = "array_mp_8188e_t_fw_nic"


def extract() -> bytes:
    text = SRC_C.read_text(errors="replace")
    # The array body runs from `u8 array_mp_8188e_t_fw_nic[] = {` to the next `};`.
    m = re.search(rf"\b{ARRAY}\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;", text, re.DOTALL)
    if not m:
        raise SystemExit(f"could not find {ARRAY}[] in {SRC_C}")
    body = m.group(1)
    bytes_out = bytes(int(tok, 16) for tok in re.findall(r"0x[0-9a-fA-F]{1,2}", body))
    # Cross-check against the declared length constant.
    lm = re.search(rf"array_length_{ARRAY}\s*=\s*(\d+)", text)
    if lm:
        declared = int(lm.group(1))
        if len(bytes_out) != declared:
            raise SystemExit(f"length mismatch: extracted {len(bytes_out)} != declared {declared}")
    return bytes_out


def main() -> int:
    blob = extract()
    OUT.write_bytes(blob)
    # Decode the 32-byte RT_8188E_FIRMWARE_HDR for a sanity print.
    sig = int.from_bytes(blob[0:2], "little")
    ver = int.from_bytes(blob[4:6], "little")
    subver = blob[6]
    print(f"wrote {OUT.relative_to(REPO)}: {len(blob)} bytes")
    print(f"  header: signature=0x{sig:04x} version={ver} subversion=0x{subver:02x}")
    print(f"  payload after 32B header: {len(blob) - 32} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
