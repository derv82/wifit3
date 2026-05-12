import sys
import pyshark
import binascii

def extract_firmware(pcap_path, output_path):
    cap = pyshark.FileCapture(pcap_path, display_filter='usb.setup.bRequest == 6 && usb.setup.wIndex >= 0x3000 && usb.setup.wIndex < 0x4000')
    
    fw_chunks = {}
    
    print("Extracting firmware chunks from PCAP...")
    for pkt in cap:
        try:
            addr = int(pkt.usb.setup_wIndex)
            data = binascii.unhexlify(pkt.usb.capdata.replace(':', ''))
            fw_chunks[addr] = data
        except AttributeError:
            continue
            
    if not fw_chunks:
        print("No firmware chunks found!")
        return
        
    # Sort by address and merge
    sorted_addrs = sorted(fw_chunks.keys())
    start_addr = sorted_addrs[0]
    last_addr = sorted_addrs[-1]
    
    full_fw = bytearray()
    expected_addr = start_addr
    
    for addr in sorted_addrs:
        if addr != expected_addr:
            # Pad with zeros if there's a gap
            gap = addr - expected_addr
            print(f"Gap found at {hex(addr)}: {gap} bytes. Padding with zeros.")
            full_fw.extend(b'\x00' * gap)
            
        full_fw.extend(fw_chunks[addr])
        expected_addr = addr + len(fw_chunks[addr])
        
    with open(output_path, 'wb') as f:
        f.write(full_fw)
        
    print(f"Firmware extracted to {output_path} ({len(full_fw)} bytes)")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python extract_rt5572_fw.py <input.pcap> <output.bin>")
    else:
        extract_firmware(sys.argv[1], sys.argv[2])
