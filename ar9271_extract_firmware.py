import sys
import struct

def extract_fw(pcap_path):
    print(f"[*] Pure Python Binary Extractor: {pcap_path}")
    firmware = bytearray()
    
    try:
        with open(pcap_path, 'rb') as f:
            # 1. Parse PCAP Global Header (24 bytes)
            global_hdr = f.read(24)
            if len(global_hdr) < 24:
                print("[-] Not a valid pcap file.")
                return
            
            magic = global_hdr[:4]
            # Detect Endianness (Standard PCAP magic numbers)
            if magic == b'\xd4\xc3\xb2\xa1':
                endian = '<'
            elif magic == b'\xa1\xb2\xc3\xd4':
                endian = '>'
            else:
                print("[-] Unknown PCAP magic. Are you sure this isn't a pcapng?")
                return

            chunk_count = 0
            
            # 2. Iterate through packets
            while True:
                pkt_hdr = f.read(16)
                if not pkt_hdr or len(pkt_hdr) < 16:
                    break # End of file
                
                # Unpack Packet Header (ts_sec, ts_usec, incl_len, orig_len)
                _, _, incl_len, _ = struct.unpack(f"{endian}IIII", pkt_hdr)
                packet_data = f.read(incl_len)
                
                # We only care about the large firmware chunks (> 1000 bytes)
                if incl_len > 1000 and len(packet_data) >= 64:
                    
                    event_type = packet_data[8]   # 0x53 ('S' for Submit)
                    xfer_type = packet_data[9]    # 2 (Control Transfer)
                    epnum = packet_data[10]       # 0 (EP 0 OUT)
                    
                    if event_type == 0x53 and xfer_type == 2 and (epnum & 0x80) == 0:
                        
                        # The Linux Kernel embeds the USB Setup packet at offset 40!
                        bmRequestType = packet_data[40]
                        bRequest = packet_data[41]
                        wValue = struct.unpack("<H", packet_data[42:44])[0]
                        wIndex = struct.unpack("<H", packet_data[44:46])[0]
                        
                        if chunk_count == 0:
                            print("\n--- PyUSB Control Transfer Parameters ---")
                            print(f"bmRequestType : {hex(bmRequestType)}")
                            print(f"bRequest      : {hex(bRequest)}")
                            print(f"wValue        : {hex(wValue)}")
                            print(f"wIndex        : {hex(wIndex)}")
                            print("-----------------------------------------\n")
                        
                        # The pure firmware payload starts exactly at byte 64
                        payload = packet_data[64:]
                        firmware.extend(payload)
                        chunk_count += 1
                        
    except Exception as e:
        print(f"[-] Error parsing binary: {e}")

    if len(firmware) > 0:
        out_path = "htc_9271_cleanroom.fw"
        with open(out_path, "wb") as f:
            f.write(firmware)
        print(f"[+] Success! Extracted {chunk_count} chunks.")
        print(f"[+] Total Firmware Size: {len(firmware)} bytes.")
        print(f"[+] Saved pristine blob to: {out_path}")
    else:
        print("[-] Failed to extract any firmware chunks.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run extract_pure.py <path_to_pcap>")
        sys.exit(1)
        
    extract_fw(sys.argv[1])