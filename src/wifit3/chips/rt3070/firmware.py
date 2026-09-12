"""Firmware load for the RT3070 / RT3072.

``rt2800usb_write_firmware`` picks the ``rt2870.bin`` section by silicon [SRC
rt2800usb.c:221-229]: offset 0 / length 4096 for RT2860/RT2872/**RT3070**, offset 4096
for all other chipsets (RT3071/**RT3072** 2T2R siblings included). Both firmware blobs
are standalone 4 KB assets. Chip=based selection in ``load_firmware_blob``:

* ``assets/rt3070_fw.bin`` — offset-0 section (md5 ``d94f0280cf9980999dbc2b999b281edb``),
  same 4 KB as RT3070's cold-boot capture.
* ``assets/rt3072_fw.bin`` — offset-4096 section (md5 ``8d98ca9f932bde2fa1fdfdb8bdd82543``),
  the 4 KB section for RT3071/RT3072.

``upload`` reproduces ``rt2800_load_firmware`` [SRC rt2800lib.c:714-792] driving
``rt2800usb_write_firmware`` [SRC rt2800usb.c:210-265].
"""
from __future__ import annotations

from pathlib import Path

from . import constants as C
from .transport import RT3070Transport

_FW_FIRST_SECTION = Path(__file__).parent / "assets" / "rt3070_fw.bin"    # RT2860/RT2872/RT3070
_FW_SECOND_SECTION = Path(__file__).parent / "assets" / "rt3072_fw.bin"   # RT3071/RT3072 (2T2R)

_FIRST_SECTION_CHIPS = (C.RT2860, C.RT2872, C.RT3070)  # [SRC rt2800usb.c:221-229]


def load_firmware_blob(chip: C.ChipInfo) -> bytes:
    """Chip-specific section of rt2870.bin."""
    if chip.rt in _FIRST_SECTION_CHIPS:
        return _FW_FIRST_SECTION.read_bytes()
    return _FW_SECOND_SECTION.read_bytes()


def _write_firmware(t: RT3070Transport, blob: bytes) -> None:
    """[SRC rt2800usb.c:210-265 rt2800usb_write_firmware]"""
    if t.autorun_detect():
        # AutoRun NIC: firmware already resident in the device, skip the upload.
        # Not this card — its autorun_detect returns 0 — but the branch is real.
        pass
    else:
        t.register_multiwrite(C.FIRMWARE_IMAGE_BASE, blob)

    t.register_write(C.H2M_MAILBOX_CID, 0xFFFFFFFF)
    t.register_write(C.H2M_MAILBOX_STATUS, 0xFFFFFFFF)

    # Tell the device to load the firmware (long-timeout vendor request).
    t.device_mode_sw(C.USB_MODE_FIRMWARE)
    # kernel msleep(10) here — replay/HW needs no settle.
    t.register_write(C.H2M_MAILBOX_CSR, 0)


def upload(t: RT3070Transport, blob: bytes) -> None:
    """Full firmware load orchestration [SRC rt2800lib.c:714-792 rt2800_load_firmware]."""
    # If driver doesn't wake firmware here, a re-up would hang forever.
    t.register_write(C.AUTOWAKEUP_CFG, 0x00000000)

    if not t.wait_csr_ready():
        raise IOError("rt3070: unstable hardware (MAC_CSR0 not ready before FW load)")

    # (PCI clock/power-pin setup is PCI-only; not taken on USB.)
    t.disable_wpdma()

    _write_firmware(t, blob)

    # Wait for the device to stabilize: PBF system register ready.
    for _ in range(C.REGISTER_BUSY_COUNT):
        reg = t.register_read(C.PBF_SYS_CTRL)
        if C.get_field(reg, C.PBF_SYS_CTRL_READY):
            break
    else:
        raise IOError("rt3070: PBF system register not ready after FW load")

    # Disable DMA; re-enabled later when the radio is enabled.
    t.disable_wpdma()

    # Initialize firmware: clear mailbox/agent, fire the boot signal (USB path).
    t.register_write(C.H2M_BBP_AGENT, 0)
    t.register_write(C.H2M_MAILBOX_CSR, 0)
    t.register_write(C.H2M_INT_SRC, 0)
    t.mcu_request(C.MCU_BOOT_SIGNAL, 0, 0, 0)
    # kernel msleep(1) here.
