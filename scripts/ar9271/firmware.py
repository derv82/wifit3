import usb.core
import time
import sys
import os

class FirmwareLoader:
    """
    Handles uploading the ath9k_htc firmware to a "cold" AR9271 device.
    """
    
    # Standard Atheros firmware upload request parameters
    BM_REQUEST_TYPE = 0x40
    B_REQUEST = 0x30
    
    @staticmethod
    def load_firmware(dev, firmware_path):
        """
        Reads the firmware file and blasts it to the device via EP0 Control Transfers.
        """
        if not os.path.exists(firmware_path):
            print(f"[-] Firmware file not found at: {firmware_path}")
            return False

        print(f"[*] Reading firmware from: {firmware_path}")
        with open(firmware_path, 'rb') as f:
            firmware_data = f.read()

        total_size = len(firmware_data)
        print(f"[*] Firmware size: {total_size} bytes")

        # WinUSB/PyUSB often struggles with 4096-byte Control Transfers,
        # leading to Errno 10060 (Timeout) or silent drops because it fails to 
        # fragment them correctly for the EP0 64-byte max packet size.
        # We will manually chunk it into smaller blocks (e.g., 512 or 64).
        # Let's try 512.
        chunk_size = 512
        
        print("[*] Beginning firmware upload...")
        
        offset = 0
        while offset < total_size:
            chunk = firmware_data[offset : offset + chunk_size]
            
            # The download address is usually 0x501000 for AR9271.
            current_addr = 0x501000 + offset
            wValue = (current_addr >> 8) & 0xFFFF
            wIndex = (current_addr >> 24) & 0xFF
            
            try:
                dev.ctrl_transfer(
                    FirmwareLoader.BM_REQUEST_TYPE, 
                    FirmwareLoader.B_REQUEST, 
                    wValue, 
                    wIndex, 
                    chunk, 
                    timeout=2000
                )
            except usb.core.USBError as e:
                print(f"\n[-] USBError during firmware upload at offset {offset}: {e}")
                return False

            sys.stdout.write(f"\r    -> Uploaded {offset + len(chunk)} / {total_size} bytes")
            sys.stdout.flush()
            
            offset += len(chunk)
            
        print("\n[+] Firmware upload complete!")
        
        print("[*] Triggering firmware boot...")
        try:
            # 1. Firmware Download Complete
            dev.ctrl_transfer(
                0x40,   # bmRequestType
                0x31,   # bRequest (FIRMWARE_DOWNLOAD_COMP / BOOT)
                0x9030, # wValue (Execution flag/address)
                0x0000, # wIndex
                b'',    # Payload
                timeout=1000
            )
            print("    -> Boot command sent (0x31).")
            
            # 2. The CPU Wakeup / Reset Latch Clear
            # Found in PCAP immediately after 0x31: bmReq 0x23, bReq 0x01, wVal 0x0010, wInd 0x0007
            dev.ctrl_transfer(
                0x23,   # bmRequestType (Class OUT)
                0x01,   # bRequest
                0x0010, # wValue (Usually a reset bit flag)
                0x0007, # wIndex (Usually a register offset)
                b'',
                timeout=1000
            )
            print("    -> CPU Wakeup sent (0x23).")
            
        except usb.core.USBError as e:
            print(f"[*] Device reset triggered (Expected USBError during boot: {e}).")
            
        return True
