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
            data = dev.read(ep_in.bEndpointAddress, buffer_size, timeout=100)
            
            # If we get here, the hardware physically received a packet!
            print(f"\n[+] BOOM! Received {len(data)} bytes from the air!")
            
            # Print the first 32 bytes to inspect the Realtek RX Descriptor
            hex_dump = ' '.join([f"{b:02x}" for b in data[:32]])
            print(f"    Raw Header: {hex_dump} ...")
            
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