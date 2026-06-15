import pytest
import usb.core

def test_mock_basic_ctrl(usb_mock):  # ar9271/conftest.py
    """Verify that a basic control transfer matches."""
    usb_mock.expect_ctrl(0x40, 0x30, 0x1234, 0x5678).respond_with(b'\x01\x02')
    
    dev = usb.core.find()
    res = dev.ctrl_transfer(0x40, 0x30, 0x1234, 0x5678)
    assert res == b'\x01\x02'

def test_mock_write_read(usb_mock):  # ar9271/conftest.py
    """Verify write and read sequence."""
    usb_mock.expect_write(0x04, b'\xaa\xbb')
    usb_mock.expect_read(0x82).respond_with(b'\xcc\xdd')
    
    dev = usb.core.find()
    dev.write(0x04, b'\xaa\xbb')
    res = dev.read(0x82, 512)
    assert res == b'\xcc\xdd'

def test_mock_mismatch_fails(usb_mock):  # ar9271/conftest.py
    """Verify that a data mismatch fails the test."""
    usb_mock.expect_write(0x04, b'\x11\x22')
    
    dev = usb.core.find()
    with pytest.raises(pytest.fail.Exception, match="Mock mismatch"):
        dev.write(0x04, b'\x33\x44')
    
    # We must manually clear expectations to avoid 'verify()' failing at teardown
    usb_mock.expectations = []

def test_mock_missing_call_fails(usb_mock):  # ar9271/conftest.py
    """Verify that verify() fails if an expected call wasn't made."""
    usb_mock.expect_write(0x04, b'\x11\x22')
    
    # Not calling dev.write()
    
    with pytest.raises(pytest.fail.Exception, match="expected calls were not made"):
        usb_mock.verify()
    
    # Clear for clean teardown
    usb_mock.expectations = []
