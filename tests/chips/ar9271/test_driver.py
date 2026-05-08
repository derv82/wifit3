import pytest
import asyncio
import usb.core
import struct
from wifit3.chips.ar9271.driver import AR9271Driver
from wifit3.chips.ar9271.constants import *

@pytest.mark.asyncio
async def test_driver_connect_and_heartbeat(usb_mock):
    """
    Test that connect() performs handshake and starts heartbeat.
    """
    # 1. Expect WMI Service Connect request
    connect_msg = bytearray.fromhex("0000000a000000000002010000000304")
    usb_mock.expect_write(USB_EP_WMI_CMD_OUT, connect_msg)
    
    # 2. Expect WMI_ECHO (Heartbeat) - first one should trigger almost immediately
    # HTC(EP=1, Len=4) + WMI(Cmd=1, Seq=1)
    # 01 00 00 04 00 00 | 00 01 00 01
    heartbeat_msg = b'\x01\x00\x00\x04\x00\x00\x00\x01\x00\x01'
    usb_mock.expect_write(USB_EP_WMI_CMD_OUT, heartbeat_msg)
    
    dev = usb.core.find()
    driver = AR9271Driver(dev)
    
    # Inject some credits so heartbeat doesn't stall
    driver.htc.update_credits(10)
    
    await driver.connect()
    
    # Wait a bit for the background tasks to run
    await asyncio.sleep(0.05)
    
    await driver.close()

@pytest.mark.asyncio
async def test_driver_set_channel(usb_mock):
    """
    Test that set_channel(6) sends the correct synthesizer word.
    """
    # Skipping connect handshake for brevity in this specific test
    dev = usb.core.find()
    driver = AR9271Driver(dev)
    driver.htc.update_credits(10)
    driver.is_running = True # Bypass connect() for direct method test
    
    # Expect WMI_REG_WRITE for AR_PHY_SYNTH_CONTROL (0x9874) with Channel 6 value (0x30a27777)
    # HTC(EP=1, Len=12) + WMI(Cmd=0x15, Seq=1) + Payload(Addr=0x9874, Val=0x30a27777)
    # EP=01, Flags=00, Len=000c, Pad=0000 | Cmd=0015, Seq=0001 | Addr=00009874, Val=30a27777
    expected_payload = b'\x00\x00\x98\x74\x30\xa2\x77\x77'
    expected_packet = b'\x01\x00\x00\x0c\x00\x00\x00\x15\x00\x01' + expected_payload
    
    usb_mock.expect_write(USB_EP_WMI_CMD_OUT, expected_packet)
    
    # Also need to mock the ACK to finish send_wmi_command
    # EP=01, Len=4, Event=0x0013, Seq=1
    ack_packet = b'\x01\x00\x00\x04\x00\x00\x00\x13\x00\x01'
    usb_mock.expect_read(USB_EP_DATA_WMI_IN).respond_with(ack_packet)
    
    # Start reader to process the ACK
    driver._reader_task = asyncio.create_task(driver._reader_loop())
    
    success = await driver.set_channel(6)
    assert success is True
    
    await driver.close()
