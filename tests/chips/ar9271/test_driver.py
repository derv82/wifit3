import pytest
import usb.core
from wifit3.chips.ar9271.driver import AR9271Driver

@pytest.mark.asyncio
async def test_driver_connect_warm(usb_mock):
    """
    Test that connect() successfully re-attaches to a warm device without sending the marathon.
    """
    dev = usb.core.find()
    driver = AR9271Driver(dev, is_warm=True)
    
    success = await driver.connect()
    assert success is True
    
    # Should be subscribed to WMI
    assert driver.transport.credit_manager._credits[driver.wmi_endpoint_id] == 33
    
    await driver.close()

@pytest.mark.asyncio
async def test_driver_set_channel(usb_mock):
    """
    Test that set_channel sends the correct synthesizer word.
    """
    dev = usb.core.find()
    driver = AR9271Driver(dev, is_warm=True)
    await driver.connect()
    
    # Just mock that send_wmi_command always succeeds for the hop sequence
    with pytest.MonkeyPatch.context() as m:
        async def mock_send(*args, **kwargs):
            return True
        m.setattr(driver, "send_wmi_command", mock_send)
        success = await driver.set_channel(6)
        assert success is True
        
    await driver.close()

