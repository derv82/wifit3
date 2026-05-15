import pytest
import asyncio
import usb.core
from wifit3.chips.rt2800usb.driver import RT2800USBDriver
from wifit3.chips.rt2800usb.transport import RT2800USBTransport

@pytest.mark.asyncio
async def test_rt2800usb_connect_warm(usb_mock):
    dev = usb.core.find()
    
    # Expected read MAC_CSR0 (0x1000)
    usb_mock.expect_ctrl(0xc0, 0x07, 0, 0x1000).respond_with([0x44, 0x33, 0x22, 0x11])
    
    # Expected read ASIC_VER_ID (0x1010)
    usb_mock.expect_ctrl(0xc0, 0x07, 0, 0x1010).respond_with([0x00, 0x00, 0x55, 0x72])
    
    # Expected read EEPROM for MAC Address (0x0002, 0x0003, 0x0004)
    # bRequest=0x09 for EEPROM read
    usb_mock.expect_ctrl(0xc0, 0x09, 0, 0x0002).respond_with([0x11, 0x22])
    usb_mock.expect_ctrl(0xc0, 0x09, 0, 0x0003).respond_with([0x33, 0x44])
    usb_mock.expect_ctrl(0xc0, 0x09, 0, 0x0004).respond_with([0x55, 0x66])
    
    transport = RT2800USBTransport(dev)
    driver = RT2800USBDriver(transport, is_warm=True, chip_id="rt5572")
    
    with pytest.MonkeyPatch.context() as m:
        from wifit3.chips.rt2800usb.assets import rt5572_init
        m.setattr(rt5572_init, "INIT_SEQ", []) # Skip the 400+ register writes
        
        async def mock_set_channel(*args, **kwargs):
            return True
        m.setattr(driver, "set_channel", mock_set_channel)
        
        await driver.connect()

    assert True
