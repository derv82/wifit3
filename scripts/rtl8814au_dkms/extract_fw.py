"""Extract the RTL8814AU NIC firmware blob from the vendor source C array.

The morrownr 8814au driver ships firmware as a C array (``array_mp_8814a_fw_nic``
in ``hal/rtl8814a/hal8814a_fw.c``) rather than a separate ``.bin`` — there is no
8814au blob in linux-firmware, so the vendor array is the source of truth. This
parses that array verbatim into ``chips/rtl8814au_dkms/assets/rtl8814au_fw.bin``
and validates the 64-byte 3081 header.

Wire byte-verification (the downloaded region equals the pcap's bulk payloads) is
done by ``verify_m1_pcap.py``; this script only does source -> blob.

Run: ``uv run python scripts/rtl8814au_dkms/extract_fw.py``
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VENDOR_FW_C = (
    REPO / "usb_dumps_new" / "captures_rtl8814au" / "driver-source"
    / "hal" / "rtl8814a" / "hal8814a_fw.c"
)
OUT = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"

ARRAY_NAME = "array_mp_8814a_fw_nic"
EXPECTED_LEN = 68320
_BYTE = re.compile(r"0x([0-9A-Fa-f]{2})")


def extract_array(text: str, name: str) -> bytes:
    start = text.index(f"{name}[] = {{")
    body = text[start:]
    end = body.index("};")
    return bytes(int(h, 16) for h in _BYTE.findall(body[:end]))


def main() -> int:
    fw = extract_array(VENDOR_FW_C.read_text(), ARRAY_NAME)

    if len(fw) != EXPECTED_LEN:
        print(f"FAIL: extracted {len(fw)} bytes, expected {EXPECTED_LEN}")
        return 1
    (sig,) = struct.unpack_from("<H", fw, 0)
    (dmem,) = struct.unpack_from("<I", fw, 36)
    (iram,) = struct.unpack_from("<I", fw, 48)
    if sig != 0x8814:
        print(f"FAIL: signature 0x{sig:04x} != 0x8814")
        return 1
    if dmem + iram + 16 + 64 != len(fw):  # +16 = two 8-byte checksum dummies
        print(f"FAIL: header sizes dmem={dmem} iram={iram} inconsistent with len {len(fw)}")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(fw)
    print(f"OK: wrote {len(fw)} bytes -> {OUT.relative_to(REPO)}")
    print(f"    signature=0x{sig:04x} dmem={dmem} iram={iram}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
