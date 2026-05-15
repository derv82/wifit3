import pytest
import asyncio
import usb.core
from unittest.mock import patch
from wifit3.chips.rtl8187.driver import RTL8187Driver

@pytest.mark.asyncio
async def test_rtl8187_connect_cold(usb_mock):
    dev = usb.core.find()
    
    # Define a tiny mock sequence
    mock_seq = [
        ("WRITE", 0x1234, 0x5678, [0xaa], 0),
        ("READ", 0x0000, 0x1111, 2, 0),
        ("WRITE_AND_WAIT", 0x2222, 0x3333, [0xbb], 0),
        ("SET_CONFIG", 0, 0, 0, 0)
    ]
    
    # 1. WRITE
    usb_mock.expect_ctrl(0x40, 5, 0x1234, 0x5678)
    
    # 2. READ
    usb_mock.expect_ctrl(0xC0, 5, 0x0000, 0x1111).respond_with([0x00, 0x00])
    
    # 3. WRITE_AND_WAIT (Write then Read ACK)
    usb_mock.expect_ctrl(0x40, 5, 0x2222, 0x3333)
    usb_mock.expect_ctrl(0xC0, 5, 0x2222, 0x3333).respond_with([0x00])

    # 4. MSR NO LINK (0x58)
    usb_mock.expect_ctrl(0x40, 5, 0x58, 0)
    
    # 5. RCR Promiscuous (0x44)
    usb_mock.expect_ctrl(0x40, 5, 0x44, 0)

    driver = RTL8187Driver(dev, is_warm=False)
    
    # Make sure we don't block in _high_res_sleep during the test
    with patch('wifit3.chips.rtl8187.sequences.init.FULL_BOOT_SEQUENCE', mock_seq), \
         patch.object(driver, '_high_res_sleep', return_value=None):
        success = await driver.connect()
        
    assert success is True
    
    # Cleanup task
    driver.is_running = False
    if driver._read_task:
        driver._read_task.cancel()
        try:
            await driver._read_task
        except asyncio.CancelledError:
            pass
