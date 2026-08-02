import subprocess

def parse_int(s):
    if not s: return 0
    if s.startswith('0x'): return int(s, 16)
    return int(s, 10)

def extract_sequence(pcap_path, limit=1000):
    cmd = [
        'tshark', '-r', pcap_path,
        '-Y', 'usb.bmRequestType == 0x40 or usb.bmRequestType == 0xc0',
        '-T', 'fields', 
        '-e', 'frame.number',
        '-e', 'usb.bmRequestType',
        '-e', 'usb.setup.bRequest',
        '-e', 'usb.setup.wValue',
        '-e', 'usb.setup.wIndex',
        '-e', 'usb.setup.wLength',
        '-e', 'usb.data_fragment'
    ]
    
    # Use a subprocess and read output
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    
    sequence = []
    count = 0
    while True:
        line = process.stdout.readline()
        if not line or count >= limit:
            break
        
        parts = line.strip().split('\t')
        if len(parts) < 6:
            continue
            
        try:
            frame_num = parts[0]
            bmReqType = parse_int(parts[1])
            bReq = parse_int(parts[2])
            wValue = parse_int(parts[3])
            wIndex = parse_int(parts[4])
            wLength = parse_int(parts[5])
            data = parts[6] if len(parts) > 6 else ""
            
            direction = "WRITE" if bmReqType == 0x40 else "READ"
            sequence.append(f"{frame_num:>5} {direction} Req:{bReq} Val:{hex(wValue)} Idx:{hex(wIndex)} Len:{wLength} Data:{data}")
            count += 1
        except Exception as e:
            continue
            
    process.terminate()
    return sequence

if __name__ == "__main__":
    seq = extract_sequence('driver_captures/rt5572/pau09n600_1.pcap', 1000)
    with open('rt5572_init_trace.txt', 'w') as f:
        for s in seq:
            f.write(s + '\n')
    print(f"Wrote {len(seq)} lines to rt5572_init_trace.txt")
