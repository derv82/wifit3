"""Firmware load for the RT5370 (RT5390).

The blob in ``assets/rt5370_fw.bin`` is the **second** 4 KB of linux-firmware's
``rt2870.bin`` (md5 ``8d98ca9f932bde2fa1fdfdb8bdd82543``), byte-verified equal to the
4 KB the kernel uploads on this card's cold-boot capture. ``rt2800usb_write_firmware``
selects offset 0 for RT2860/RT2872/RT3070 and **offset 4096 for everyone else**
(RT5390 included) [SRC rt2800usb.c:218-244]; we pre-extract the RT5390 section so
``upload`` is a straight multiwrite.

``upload`` reproduces ``rt2800_load_firmware`` [SRC rt2800lib.c:714-792] driving
``rt2800usb_write_firmware`` [SRC rt2800usb.c:210-265].
"""
from __future__ import annotations

from pathlib import Path

from . import constants as C
from .transport import RT5370Transport

_FW_PATH = Path(__file__).parent / "assets" / "rt5370_fw.bin"


def load_firmware_blob() -> bytes:
    return _FW_PATH.read_bytes()


def _write_firmware(t: RT5370Transport, blob: bytes) -> None:
    """[SRC rt2800usb.c:210-265 rt2800usb_write_firmware]"""
    if t.autorun_detect():
        # AutoRun NIC: firmware already resident in the device, skip the upload.
        # Not this card — its autorun_detect returns 0 — but the branch is real.
        pass
    else:
        # RT5390 ⇒ offset 4096, length 4096; the asset is already that section.
        t.register_multiwrite(C.FIRMWARE_IMAGE_BASE, blob)

    t.register_write(C.H2M_MAILBOX_CID, 0xFFFFFFFF)
    t.register_write(C.H2M_MAILBOX_STATUS, 0xFFFFFFFF)

    # Tell the device to load the firmware (long-timeout vendor request).
    t.device_mode_sw(C.USB_MODE_FIRMWARE)
    # kernel msleep(10) here — replay/HW needs no settle.
    t.register_write(C.H2M_MAILBOX_CSR, 0)


def upload(t: RT5370Transport, blob: bytes) -> None:
    """Full firmware load orchestration [SRC rt2800lib.c:714-792 rt2800_load_firmware]."""
    # If driver doesn't wake firmware here, uploading it again would hang forever.
    t.register_write(C.AUTOWAKEUP_CFG, 0x00000000)

    if not t.wait_csr_ready():
        raise IOError("rt5370: unstable hardware (MAC_CSR0 not ready before FW load)")

    # (PCI clock/power-pin setup is PCI-only; not taken on USB.)
    t.disable_wpdma()

    _write_firmware(t, blob)

    # Wait for the device to stabilize: PBF system register ready.
    for _ in range(C.REGISTER_BUSY_COUNT):
        reg = t.register_read(C.PBF_SYS_CTRL)
        if C.get_field(reg, C.PBF_SYS_CTRL_READY):
            break
    else:
        raise IOError("rt5370: PBF system register not ready after FW load")

    # Disable DMA; re-enabled later when the radio is enabled.
    t.disable_wpdma()

    # Initialize firmware: clear mailbox/agent, fire the boot signal (USB path).
    t.register_write(C.H2M_BBP_AGENT, 0)
    t.register_write(C.H2M_MAILBOX_CSR, 0)
    t.register_write(C.H2M_INT_SRC, 0)
    t.mcu_request(C.MCU_BOOT_SIGNAL, 0, 0, 0)
    # kernel msleep(1) here.
