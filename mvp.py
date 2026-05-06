import usb.core
import usb.util
import time
import sys

def main():
    print("[*] Wifite3 MVP: RTL8187L Hardware Bypass")
    
    # 1. Find the AWUS036H (Must be bound to WinUSB via Zadig)
    dev = usb.core.find(idVendor=0x0bda, idProduct=0x8187)
    if dev is None:
        print("[-] Device not found. Is it plugged in and using the WinUSB driver?")
        sys.exit(1)

    print("[+] Device claimed successfully via PyUSB.")
    
    # Set the active configuration. With no arguments, the first configuration will be the active one
    dev.set_configuration()

    # Helper function for 1-byte register writes
    def write_reg8(reg, val):
        # 0x40 = Vendor Host-to-Device | 5 = Write Command
        dev.ctrl_transfer(0x40, 5, reg, 0, [val])
        time.sleep(0.005) # 5ms stabilization delay

    # Helper function for 4-byte (32-bit) register writes (like Monitor Mode)
    def write_reg32(reg, val_array):
        dev.ctrl_transfer(0x40, 5, reg, 0, val_array)
        time.sleep(0.01)

    try:
        # =================================================================
        # THE INCANTATION
        # =================================================================
        
        # 1. Clear / Reset (Targeting Command Register 0xff50)
        # We write 0xc0 to turn off everything, then 0x8c to enable RX/TX
        print("[*] Sending Reset & Enable commands...")
        write_reg8(0xff50, 0xc0)
        time.sleep(0.05)
        write_reg8(0xff50, 0x8c)
        
        # 2. Enter Monitor Mode (Targeting Receive Configuration 0xff44)
        print("[*] Bypassing MAC Hardware Filter (Monitor Mode)...")
        # 0bfc9c90 is little-endian for the promisc/multicast/broadcast bitmask
        write_reg32(0xff44, [0x90, 0x9c, 0xfc, 0x0b])
        
        # 3. Tune to Channel 1 (The Baseband "Zebra" Sequence)
        print("[*] Tuning RF Synthesizer to Channel 1...")
        
        # PASTE YOUR FULL TSHARK OUTPUT HERE!
        # Format: (Register, Data Byte)
        ch1_sequence = [
            (0xff7d, 0x36),
            (0xff7c, 0xc4),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x35),
            (0xff7c, 0xc5),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x2e),
            (0xff7c, 0xc6),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x25),
            (0xff7c, 0xc7),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x1c),
            (0xff7c, 0xc8),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x12),
            (0xff7c, 0xc9),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x09),
            (0xff7c, 0xca),
            (0xff7f, 0x01),
            (0xff7e, 0x00),
            (0xff7d, 0x04),
        ]
        
        for reg, val in ch1_sequence:
            write_reg8(reg, val)

        print("\n[+] SUCCESS! The card is now listening passively on Channel 1.")
        print("[+] OS Networking Stack is completely bypassed.")
        
    except usb.core.USBError as e:
        print(f"[-] USB Error during incantation: {e}")

if __name__ == "__main__":
    main()
