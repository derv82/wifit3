import subprocess
import binascii

def extract():
    cmd = [
        'tshark', '-r', 'usb_dumps/rt5572/pau09n600_1.pcap',
        '-Y', 'usb.setup.wIndex >= 12288 and usb.setup.wIndex < 16384 and usb.setup.bRequest == 6',
        '-T', 'fields', '-e', 'usb.setup.wIndex', '-e', 'usb.data_fragment'
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    
    if stderr:
        print(f"Error: {stderr}")
        
    fw_chunks = {}
    for line in stdout.splitlines():
        parts = line.strip().split('\t')
        if len(parts) < 2:
            continue
        addr = int(parts[0])
        data = binascii.unhexlify(parts[1].replace(':', ''))
        fw_chunks[addr] = data
        
    if not fw_chunks:
        print("No chunks found!")
        return
        
    sorted_addrs = sorted(fw_chunks.keys())
    full_fw = bytearray()
    expected_addr = sorted_addrs[0]
    
    for addr in sorted_addrs:
        if addr > expected_addr:
            full_fw.extend(b'\x00' * (addr - expected_addr))
        full_fw.extend(fw_chunks[addr])
        expected_addr = addr + len(fw_chunks[addr])
        
    with open('rt5572.bin', 'wb') as f:
        f.write(full_fw)
    print(f"Extracted {len(full_fw)} bytes to rt5572.bin")

if __name__ == "__main__":
    extract()
