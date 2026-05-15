import pytest
import usb.core
from wifit3.chips.ar9271.firmware import FirmwareLoader

def test_firmware_load_sequence(usb_mock):
    """
    Verify the full firmware upload sequence: Chunks -> Boot -> Wakeup.
    """
    # 1024 bytes = 2 chunks of 512
    dummy_fw = b'\xaa' * 512 + b'\xbb' * 512
    
    # Expect Chunk 1 (Addr 0x501000)
    # wValue = (0x501000 >> 8) & 0xFFFF = 0x5010
    # wIndex = (0x501000 >> 24) & 0xFF = 0x00
    usb_mock.expect_ctrl(0x40, 0x30, 0x5010, 0x00, data=b'\xaa' * 512)
    
    # Expect Chunk 2 (Addr 0x501200)
    # wValue = (0x501200 >> 8) & 0xFFFF = 0x5012
    # wIndex = (0x501200 >> 24) & 0xFF = 0x00
    usb_mock.expect_ctrl(0x40, 0x30, 0x5012, 0x00, data=b'\xbb' * 512)
    
    # Expect Boot Trigger (0x31)
    usb_mock.expect_ctrl(0x40, 0x31, 0x9030, 0x00, data=b'')

    dev = usb.core.find()
    success = FirmwareLoader.load(dev, dummy_fw)
    assert success is True
def test_firmware_load_reset_error_is_success(usb_mock):
    """
    Verify that a USBError during boot trigger is treated as success (device resetting).
    """
    dummy_fw = b'\xff' * 10
    
    # Chunk 1
    usb_mock.expect_ctrl(0x40, 0x30, 0x5010, 0x00, data=b'\xff' * 10)
    
    # Boot Trigger - simulate a reset error
    usb_mock.expect_ctrl(0x40, 0x31, 0x9030, 0x00, data=b'').error_with(usb.core.USBError("Pipe error"))
    
    dev = usb.core.find()
    success = FirmwareLoader.load(dev, dummy_fw)
    assert success is True
