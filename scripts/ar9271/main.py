import libusb_package
import usb.core
import usb.util
import time
import sys
import os
import struct
import traceback

from usb_manager import USBManager
from scanner import AR9271Scanner
from firmware import FirmwareLoader

def main():
    print("[*] Wifit3 AR9271: Robust Architecture MVP")
    
    libusb_backend = libusb_package.get_libusb1_backend()
    if libusb_backend is None:
        print("[-] Fatal: Could not load the libusb backend.")
        sys.exit(1)

    dev = usb.core.find(backend=libusb_backend, idVendor=0x0cf3, idProduct=0x9271)
    if dev is None:
        print("[-] Device not found (0x0cf3:0x9271). Is it plugged in and using WinUSB?")
        sys.exit(1)

    print("[+] Device found via PyUSB.")
    
    # Firmware Check & Upload Phase
    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]
    
    # A cold AR9271 (no firmware) has EP4 as an Interrupt OUT endpoint.
    # Once the firmware is loaded, EP4 changes to a Bulk OUT endpoint.
    ep4 = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == 0x04)
    
    is_cold = False
    if ep4 is not None:
        if usb.util.endpoint_type(ep4.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR:
            is_cold = True
    else:
        # If EP4 is entirely missing, it's also definitely cold or in a weird state.
        is_cold = True

    if is_cold:
        print("[*] Device appears uninitialized (EP4 is missing or Interrupt). Initiating firmware upload...")
        fw_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "htc_9271_cleanroom.fw")
        success = FirmwareLoader.load_firmware(dev, fw_path)
        if success:
            print("[*] Firmware uploaded and boot command sent.")
        else:
            print("[-] Firmware upload failed.")
            sys.exit(1)
    else:
        print("[+] Firmware appears to be already loaded.")

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        if e.errno == 13:
            pass # Windows might block this if already configured, that's fine
        else:
            print(f"[-] Could not set configuration: {e}")
            sys.exit(1)

    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]
    
    # Explicitly target EP 0x04 for WMI Control OUT (It is an Interrupt Endpoint!)
    ep_out = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and 
                                     usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR and
                                     e.bEndpointAddress == 0x04
    )
    
    # Explicitly target EP 0x83 for HTC Control IN (Interrupt)
    ep_htc_in = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and 
                                     usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR and
                                     e.bEndpointAddress == 0x83
    )

    # Explicitly target EP 0x81 or 0x82 for WMI Events/RX Data IN (Bulk)
    ep_in = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and 
                                     usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK and
                                     e.bEndpointAddress in (0x81, 0x82)
    )
    
    if ep_out is None or ep_in is None or ep_htc_in is None:
        print("[-] Could not find required endpoints (EP4 OUT, EP83 IN, or EP82 IN).")
        sys.exit(1)

    print(f"[+] Found EP OUT: {hex(ep_out.bEndpointAddress)}, EP IN (Data): {hex(ep_in.bEndpointAddress)}, EP IN (HTC): {hex(ep_htc_in.bEndpointAddress)}")

    # =========================================================================
    # HTC CONNECT HANDSHAKE
    # =========================================================================
    print("[*] Waiting for HTC Ready on EP83...")
    try:
        # Read the 16-byte HTC Ready message
        ready_data = bytes(dev.read(ep_htc_in.bEndpointAddress, 512, timeout=2000))
        if len(ready_data) >= 10 and ready_data[8:10] == b'\x00\x01':
            print("[+] Target is Ready! (HTC_MSG_READY_ID)")
        else:
            print(f"[-] Unexpected data on EP83 instead of HTC Ready: {ready_data.hex(' ')}")
            sys.exit(1)
            
        print("[*] Connecting WMI Service (0x0100)...")
        # HTC_MSG_CONNECT_SERVICE_ID (0x0002)
        # EP=0, Flags=0, Len=10, Pad=4, Msg=2, Svc=0x0100, ConnFlags=0, Meta=0
        htc_connect_req = bytearray.fromhex("0000000a000000000002010000000304")
        dev.write(ep_out.bEndpointAddress, htc_connect_req, timeout=1000)
        
        # Wait for HTC_MSG_CONNECT_SERVICE_RESPONSE_ID (0x0003)
        conn_resp = bytes(dev.read(ep_htc_in.bEndpointAddress, 512, timeout=1000))
        if len(conn_resp) >= 12 and conn_resp[8:10] == b'\x00\x03':
            assigned_ep = conn_resp[13]
            print(f"[+] WMI Service Connected. Assigned to HTC Endpoint: {assigned_ep:02x}")
        else:
            print(f"[-] Failed to get Connect Service Response: {conn_resp.hex(' ')}")
            sys.exit(1)
            
    except usb.core.USBError as e:
        print(f"[-] HTC Handshake Failed: {e}")
        sys.exit(1)

    usb_mgr = USBManager(dev, ep_out, ep_in)
    scanner = AR9271Scanner(usb_mgr)

    try:
        # 1. Start the single-consumer background thread
        print("\n[*] Starting USB single-consumer thread...")
        usb_mgr.start()
        
        # Give the reader thread a moment to clear any pending halts
        time.sleep(0.5)
        
        # 2. Hop to Channel 6
        scanner.hop_channel(6)
        
        print("\n[*] Sniffing on Channel 6 for 10 seconds...")
        start_time = time.time()
        while time.time() - start_time < 10:
            scanner.process_rx_queue(timeout=0.5)
        
        # 3. Hop to Channel 1
        print("\n")
        scanner.hop_channel(1)
        
        print("\n[*] Sniffing on Channel 1 for 10 seconds...")
        start_time = time.time()
        while time.time() - start_time < 10:
            scanner.process_rx_queue(timeout=0.5)
            
    except usb.core.USBError as e:
        print(f"\n[-] USB Error: {e}")
        traceback.print_exc()
    except KeyboardInterrupt:
        print("\n[*] Exiting...")
    finally:
        print("[*] Shutting down USB thread...")
        usb_mgr.stop()

if __name__ == "__main__":
    main()
