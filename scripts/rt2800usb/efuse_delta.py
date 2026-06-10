"""EFUSE byte-vs-word ADDRESS_IN delta probe for rt2800usb.

The shipping ``read_eeprom_efuse`` writes ``EFUSE_CTRL.ADDRESS_IN = byte_offset``
(0, 16, 32, ...), but the EFUSE treats ADDRESS_IN as a u16-**word** index — the
kernel (``rt2800lib.c`` ``rt2800_read_eeprom_efuse`` / ``rt2800_efuse_read``)
loops ``i += 8`` *words* and writes ``ADDRESS_IN = i``. So every 16-byte block
past block 0 is fetched from *double* the fuse address, and blocks past byte 256
run off the end of the 512-byte fuse. Block 0 (MAC, bytes 0-15) reads correctly
either way, which masked the bug.

This probe reads the fuse BOTH ways on the plugged-in card and diffs the
RX-relevant fields — NIC_CONF0 (chain counts), freq_offset, LNA gain, RSSI
offsets, NIC_CONF1 ext-LNA bits — so the handicap can be quantified *before*
the fix lands.

Read-only: only EFUSE_CTRL + EFUSE_DATA reads. No fuse writes, no bring-up
state change. Safe on a warm card. Run with the card plugged + WinUSB-bound:

    uv run python scripts/rt2800usb/efuse_delta.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rt2800usb.constants import (
    USB_PID_RT3572,
    USB_PID_RT5372,
    USB_PID_RT5572,
    USB_VID_RALINK,
)
from wifit3.chips.rt2800usb.eeprom import (
    EEPROM_OFFSET_NIC_CONF0,
    EEPROM_OFFSET_NIC_CONF1,
    EEPROM_OFFSET_FREQ,
    EEPROM_OFFSET_LNA,
    EEPROM_OFFSET_RSSI_BG,
    EEPROM_SIZE,
    EFUSE_READ_CHUNK,
    _efuse_read_chunk,
    _word,
    efuse_detect,
    parse_eeprom,
    read_eeprom_efuse,
)
from wifit3.chips.rt2800usb.mac import read_chip_id
from wifit3.chips.rt2800usb.transport import RT2800USBTransport


def open_device():
    """Find any of RT3572/RT5372/RT5572 plugged in. First-match wins.
    Mirrors test_hw_rt2800usb.open_device (kept standalone on purpose)."""
    backend = libusb_package.get_libusb1_backend()
    for pid, label in (
        (USB_PID_RT5372, "RT5372 (Panda PAU05)"),
        (USB_PID_RT3572, "RT3572 (ALFA AWUS051NH v2)"),
        (USB_PID_RT5572, "RT5572 (Panda PAU09 N600)"),
    ):
        dev = usb.core.find(idVendor=USB_VID_RALINK, idProduct=pid, backend=backend)
        if dev is not None:
            print(f"  Found {label} at bus {dev.bus}, address {dev.address}")
            break
    else:
        sys.exit(
            "No rt2800usb dongle found (VID 0x148f, PID 0x5372/0x3572/0x5572). "
            "Plug one in, confirm Zadig bound it to WinUSB, and retry."
        )

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    return dev


def read_eeprom_corrected(t: RT2800USBTransport) -> bytes:
    """Kernel-faithful read: ADDRESS_IN is the u16-word index (byte // 2).
    Storage block stays at the same byte offset; only the fetched fuse
    address is corrected. Mirrors the proposed fix to read_eeprom_efuse."""
    if not efuse_detect(t):
        raise IOError("EFUSE_CTRL.PRESENT not set")
    buf = bytearray(EEPROM_SIZE)
    for offset in range(0, EEPROM_SIZE, EFUSE_READ_CHUNK):
        buf[offset: offset + EFUSE_READ_CHUNK] = _efuse_read_chunk(t, offset // 2)
    return bytes(buf)


def _hex(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def _decode(img: bytes) -> dict:
    """Raw field values straight from the image — no unburned-default
    substitution (parse_eeprom applies that; we want the fuse truth)."""
    nic0 = _word(img, EEPROM_OFFSET_NIC_CONF0)
    nic1 = _word(img, EEPROM_OFFSET_NIC_CONF1)
    freq = _word(img, EEPROM_OFFSET_FREQ) & 0xFF
    lna = _word(img, EEPROM_OFFSET_LNA)
    rssi = _word(img, EEPROM_OFFSET_RSSI_BG)
    return {
        "nic_conf0": nic0,
        "rxpath": nic0 & 0x000F,
        "txpath": (nic0 & 0x00F0) >> 4,
        "rf_type": (nic0 & 0x0F00) >> 8,
        "nic_conf1": nic1,
        "ext_lna_bg": bool(nic1 & 0x0100),
        "ext_lna_a": bool(nic1 & 0x0200),
        "freq_offset": freq,
        "lna_gain_bg": lna & 0xFF,
        "lna_gain_a": (lna >> 8) & 0xFF,
        "rssi_bg0": rssi & 0xFF,
        "rssi_bg1": (rssi >> 8) & 0xFF,
    }


def main() -> None:
    print("[*] Opening rt2800usb dongle (read-only EFUSE probe)...")
    dev = open_device()
    t = RT2800USBTransport(dev)
    try:
        chip = read_chip_id(t)
        print(f"  chip = {chip.name}  silicon=0x{chip.silicon_id:04x}  rev=0x{chip.revision:04x}\n")

        buggy = read_eeprom_efuse(t)        # current shipping reader (byte ADDRESS_IN)
        corrected = read_eeprom_corrected(t)  # kernel-faithful (word ADDRESS_IN)

        # Sanity: block 0 (MAC + first 16 bytes) must be identical both ways.
        same_block0 = buggy[:16] == corrected[:16]
        print(f"  block 0 (bytes 0x00-0x0f) identical both ways: {same_block0}  "
              f"{'(expected — MAC reads correct)' if same_block0 else '(UNEXPECTED)'}")

        b, c = _decode(buggy), _decode(corrected)

        print("\n  Field-level diff — current(buggy byte-addr) vs corrected(word-addr):")
        print(f"  {'field':<16} {'buggy':>10} {'corrected':>12}   change")
        print("  " + "-" * 56)
        rows = [
            ("NIC_CONF0", f"0x{b['nic_conf0']:04x}", f"0x{c['nic_conf0']:04x}"),
            ("  rxpath", str(b["rxpath"]), str(c["rxpath"])),
            ("  txpath", str(b["txpath"]), str(c["txpath"])),
            ("  rf_type", str(b["rf_type"]), str(c["rf_type"])),
            ("NIC_CONF1", f"0x{b['nic_conf1']:04x}", f"0x{c['nic_conf1']:04x}"),
            ("  ext_lna_bg", str(b["ext_lna_bg"]), str(c["ext_lna_bg"])),
            ("  ext_lna_a", str(b["ext_lna_a"]), str(c["ext_lna_a"])),
            ("freq_offset", f"0x{b['freq_offset']:02x} ({b['freq_offset']})",
             f"0x{c['freq_offset']:02x} ({c['freq_offset']})"),
            ("lna_gain_bg", f"0x{b['lna_gain_bg']:02x}", f"0x{c['lna_gain_bg']:02x}"),
            ("lna_gain_a", f"0x{b['lna_gain_a']:02x}", f"0x{c['lna_gain_a']:02x}"),
            ("rssi_bg0", f"0x{b['rssi_bg0']:02x}", f"0x{c['rssi_bg0']:02x}"),
            ("rssi_bg1", f"0x{b['rssi_bg1']:02x}", f"0x{c['rssi_bg1']:02x}"),
        ]
        for name, bv, cv in rows:
            mark = "" if bv == cv else "  <-- DIFF"
            print(f"  {name:<16} {bv:>10} {cv:>12}{mark}")

        # Raw 16-byte blocks where the fields live (block 3 = 0x30-0x3f,
        # block 4 = 0x40-0x4f) — shows the address shift directly.
        print("\n  Raw fuse blocks holding these fields (device cal bytes):")
        for blk in (0x30, 0x40):
            print(f"    bytes 0x{blk:02x}-0x{blk+15:02x}  buggy     : {_hex(buggy[blk:blk+16])}")
            print(f"    bytes 0x{blk:02x}-0x{blk+15:02x}  corrected : {_hex(corrected[blk:blk+16])}")

        # What parse_eeprom (with unburned-default substitution) actually
        # feeds the driver today vs after the fix.
        pe_b, pe_c = parse_eeprom(buggy), parse_eeprom(corrected)
        print("\n  parse_eeprom() output (post unburned-default substitution):")
        print(f"    freq_offset   driver-today={pe_b.freq_offset:>3}   after-fix={pe_c.freq_offset:>3}")
        print(f"    lna_gain_bg   driver-today=0x{pe_b.lna_gain_bg:02x}   after-fix=0x{pe_c.lna_gain_bg:02x}")
        print(f"    rxpath/txpath driver-today={pe_b.rxpath}/{pe_b.txpath}    after-fix={pe_c.rxpath}/{pe_c.txpath}")
    finally:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
