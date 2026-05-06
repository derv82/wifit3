import libusb_package
import usb.core
import usb.util
import time
import sys
import threading
import traceback

def read_bulk_in(dev, ep_in):
    """Continuously polls the Bulk IN endpoint for received Wi-Fi frames."""
    print(f"[*] Started async listener on Endpoint {hex(ep_in.bEndpointAddress)}...")
    buffer_size = 4096
    
    while True:
        try:
            # We use a short timeout so the thread doesn't lock up if the air is quiet
            raw_array = dev.read(ep_in.bEndpointAddress, buffer_size, timeout=100)
            data = bytes(raw_array)  # Convert PyUSB array.array to native bytes
            # If we get here, the hardware physically received a packet!
            
            # Format MAC address helper
            def mac_str(mac_bytes):
                return ':'.join(f'{b:02x}' for b in mac_bytes)

            if len(data) >= 10:
                fc = data[0]
                frame_type = (fc >> 2) & 0x03
                frame_subtype = (fc >> 4) & 0x0f

                types = {0: "Mgt", 1: "Ctrl", 2: "Data"}
                type_str = types.get(frame_type, f"Type {frame_type}")
                sub_str = f"Sub {frame_subtype}"

                # Beacons, Probes, and Data frames (typically 24 byte header)
                if frame_type in [0, 2] and len(data) >= 24:
                    if frame_type == 0 and frame_subtype == 8: sub_str = "Beacon"
                    elif frame_type == 0 and frame_subtype == 4: sub_str = "ProbeReq"
                    elif frame_type == 0 and frame_subtype == 12: sub_str = "Deauth"
                    elif frame_type == 2: sub_str = "Data"
                    
                    addr1 = mac_str(data[4:10])  # Destination (DA)
                    addr2 = mac_str(data[10:16]) # Source (SA)
                    addr3 = mac_str(data[16:22]) # BSSID
                    
                    # 1. SSID Extraction (Parsing 802.11 Information Elements)
                    ssid = ""
                    if frame_type == 0 and frame_subtype in [4, 8]: # Probes and Beacons
                        # Beacons have 12 bytes of fixed params after the header. Probes have 0.
                        offset = 36 if frame_subtype == 8 else 24
                        
                        # Loop through the tags until we find Tag Number 0 (SSID)
                        while offset < len(data) - 1:
                            tag_num = data[offset]
                            tag_len = data[offset+1]
                            
                            if tag_num == 0: 
                                ssid_bytes = data[offset+2 : offset+2+tag_len]
                                ssid = ssid_bytes.decode('utf-8', errors='ignore')
                                break
                                
                            offset += 2 + tag_len # Jump to the next tag
                    
                    # 2. Monitor Mode Proof
                    # If the destination isn't Broadcast, the hardware filter is bypassed.
                    proof = ""
                    if addr1 != "ff:ff:ff:ff:ff:ff":
                        proof = "  [🎯 PROOF OF MONITOR MODE: Caught 3rd-party Unicast]"

                    ssid_display = f" | SSID: '{ssid}'" if ssid else ""
                    print(f"    -> 802.11 {type_str:<4} ({sub_str:<8}) | DA: {addr1} | SA: {addr2}{ssid_display}{proof}")
                
                # Control frames (typically 10-16 byte header)
                elif frame_type == 1:
                    if frame_subtype == 13: sub_str = "ACK"
                    elif frame_subtype == 11: sub_str = "RTS"
                    elif frame_subtype == 12: sub_str = "CTS"
                    
                    addr1 = mac_str(data[4:10])  # Destination (DA)
                    
                    proof = ""
                    if addr1 != "ff:ff:ff:ff:ff:ff":
                        proof = "  [🎯 PROOF OF MONITOR MODE]"
                        
                    print(f"    -> 802.11 {type_str:<4} ({sub_str:<8}) | DA: {addr1} {proof}")
            
        except usb.core.USBError as e:
            # Print the actual string of the error
            err_str = str(e).lower()
            if 'timeout' not in err_str and e.errno not in (110, 10060):
                print(f"[-] USB Read Error: {e} (Errno {e.errno})")
                break
        except Exception as e:
            print(f"[-] Async Reader Exception: {e}")
            break

def main():
    print("[*] Wifite3 MVP: RTL8187L Hardware Bypass (Passive Sniffing)")
    
    # 0. Explicitly load the libusb backend for Windows
    print("[*] Loading libusb backend...")
    libusb_backend = libusb_package.get_libusb1_backend()
    if libusb_backend is None:
        print("[-] Fatal: Could not load the libusb backend. Your environment might be missing the DLL.")
        sys.exit(1)

    # 2. Find the AWUS036H (Pass the backend explicitly here!)
    # NOTE: Zadig must have replaced the Realtek driver with WinUSB!
    dev = usb.core.find(backend=libusb_backend, idVendor=0x0bda, idProduct=0x8187)
    if dev is None:
        print("[-] Device not found. Is it plugged in and using the WinUSB driver?")
        sys.exit(1)

    print("[+] Device claimed successfully via PyUSB.")
    
    for cfg in dev:
        for intf in cfg:
            print(f"  Interface {intf.bInterfaceNumber}, Alt {intf.bAlternateSetting}")
            for ep in intf:
                print(f"    Endpoint Address: {hex(ep.bEndpointAddress)}")
                print(f"      Type: {usb.util.endpoint_type(ep.bmAttributes)}")

    # Set the active configuration
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        print(f"[-] Could not set configuration. Is another program using it? Error: {e}")
        sys.exit(1)

    # Find the Bulk IN endpoint for our async reader
    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]
    ep_in = usb.util.find_descriptor(
        intf,
        custom_match = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and 
                                 usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
    )
    
    if ep_in is None:
        print("[-] Could not find Bulk IN endpoint!")
        sys.exit(1)

    # Helper function for 1-byte register writes
    def write_reg8(reg, val):
        # 0x40 = Vendor Host-to-Device, 5 = Write Command
        dev.ctrl_transfer(0x40, 5, reg, 0, [val])
        time.sleep(0.005)

    # Helper function for 4-byte (32-bit) register writes
    def write_reg32(reg, val_array):
        dev.ctrl_transfer(0x40, 5, reg, 0, val_array)
        time.sleep(0.01)

    try:
        # =================================================================
        # PHASE 1: THE HARDWARE INCANTATION (Time-Accurate)
        # =================================================================
        try:
            from boot_sequence import FULL_BOOT_SEQUENCE
        except ImportError:
            print("[-] Fatal: Could not find boot_sequence.py.")
            sys.exit(1)

        total_cmds = len(FULL_BOOT_SEQUENCE)
        print(f"[*] Blasting {total_cmds} interleaved commands (Writes + Reads) with micro-delays...")
        
        for i, (cmd_type, wVal, wIdx, data, delay) in enumerate(FULL_BOOT_SEQUENCE):
            sys.stdout.write(f"\r    -> Firing: [ {i+1} / {total_cmds} ] ")
            sys.stdout.flush()

            try:
                if cmd_type == "WRITE":
                    dev.ctrl_transfer(0x40, 5, wVal, wIdx, data)
                
                elif cmd_type == "READ":
                    # data variable holds the wLength (number of bytes to read)
                    dev.ctrl_transfer(0xc0, 5, wVal, wIdx, data)
                
                elif cmd_type == "SET_CONFIG":
                    dev.set_configuration()
                    
            except usb.core.USBError as e:
                # Ignore harmless WinUSB warnings
                if cmd_type == "SET_CONFIG": pass
                else: raise e
                
            time.sleep(delay) 

        print("\n\n[+] Boot Sequence Complete. Latches cleared, Baseband calibrated.")
        
        # =================================================================
        # PHASE 2: PASSIVE SNIFFING
        # =================================================================
        
        print("[*] Flushing USB FIFO Buffers...")
        try:
            dev.clear_halt(ep_in.bEndpointAddress)
        except Exception:
            pass # Completely normal if endpoint isn't stalled yet

        # Start the background thread
        listener_thread = threading.Thread(target=read_bulk_in, args=(dev, ep_in), daemon=True)
        listener_thread.start()
        
        print("[*] Keeping main thread alive. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            
    except usb.core.USBError as e:
        print(f"\n[-] USB Error during incantation: {e}")
        traceback.print_exc()
    except KeyboardInterrupt:
        print("\n[*] Exiting Wifite3 MVP...")

if __name__ == "__main__":
    main()