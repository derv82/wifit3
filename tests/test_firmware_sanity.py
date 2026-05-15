import os
from pathlib import Path

def test_ar9271_firmware_exists():
    # Construct path identical to how the manager does it
    base_dir = Path(__file__).parent.parent
    fw_path = base_dir / "src" / "wifit3" / "chips" / "ar9271" / "assets" / "htc_9271_cleanroom.fw"
    
    assert fw_path.exists(), f"AR9271 Firmware missing at {fw_path}"
    assert fw_path.stat().st_size > 10000, "AR9271 Firmware file is too small to be valid."

def test_rt2800usb_firmware_exists():
    base_dir = Path(__file__).parent.parent
    fw_path = base_dir / "src" / "wifit3" / "chips" / "rt2800usb" / "assets" / "rt5572.bin"
    
    assert fw_path.exists(), f"RT2800USB Firmware missing at {fw_path}"
    assert fw_path.stat().st_size > 1000, "RT2800USB Firmware file is too small to be valid."
