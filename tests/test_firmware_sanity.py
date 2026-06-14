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

def test_mt7921au_firmware_exists():
    base_dir = Path(__file__).parent.parent
    assets_dir = base_dir / "src" / "wifit3" / "chips" / "mt7921au" / "assets"
    fw_wm = assets_dir / "WIFI_MT7961_patch_mcu_1_2_hdr.bin"
    fw_rom = assets_dir / "WIFI_RAM_CODE_MT7961_1.bin"

    assert fw_wm.exists(), f"MT7921AU Firmware missing at {fw_wm}"
    assert fw_rom.exists(), f"MT7921AU Firmware missing at {fw_rom}"
    assert fw_wm.stat().st_size > 10000, "MT7921AU Firmware file is too small to be valid."
    assert fw_rom.stat().st_size > 100000, "MT7921AU Firmware file is too small to be valid."
