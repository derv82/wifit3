import subprocess
import os

PCAPS = [
    r"driver_captures\round2\awus036h_1.pcap",
    r"driver_captures\round2\awus036h_2.pcap",
    r"driver_captures\round2\awus036h_3.pcap"
]

def extract_all_writes(pcap_file):
    # Filter: Any Host-to-Device control transfer (Standard 0x00, Class 0x20, Vendor 0x40)
    cmd = [
        "tshark.exe", "-r", pcap_file,
        "-Y", "frame.number <= 16400 and usb.bmRequestType <= 0x7F",
        "-T", "fields",
        "-e", "usb.bmRequestType",
        "-e", "usb.setup.bRequest",
        "-e", "usb.setup.wValue",
        "-e", "usb.setup.wIndex",
        "-e", "usb.data_fragment",
        "-e", "usb.capdata",
        "-E", "separator=|"
    ]
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("[-] tshark.exe not found.")
        return []

    sequence = []
    for line in process.stdout:
        parts = line.strip('\n').split('|')
        if len(parts) < 4: continue
        
        bmReq = parts[0]
        bReq = parts[1]
        wVal = parts[2]
        wIdx = parts[3]
        
        payload = parts[4] if len(parts) > 4 and parts[4] else (parts[5] if len(parts) > 5 else "")
        payload = payload.replace(':', '').lower()
        
        if bmReq and bReq:
            sequence.append((bmReq, bReq, wVal, wIdx, payload))
            
    return sequence

def main():
    print("[*] Starting Unfiltered Differential Analysis...\n")
    sequences = []
    
    for pcap in PCAPS:
        if not os.path.exists(pcap):
            print(f"[-] Missing {pcap}")
            return
            
        seq = extract_all_writes(pcap)
        print(f"[+] {pcap}: {len(seq)} total Host-to-Device commands extracted.")
        sequences.append(seq)
        
    l1, l2, l3 = len(sequences[0]), len(sequences[1]), len(sequences[2])
    if l1 == l2 == l3:
        print(f"\n[+] Length Check: PASSED ({l1} commands).")
    else:
        print(f"\n[-] Length Check: FAILED. Lengths differ ({l1} vs {l2} vs {l3}).")
        
    print("[*] Performing byte-by-byte comparison across all fields...")
    min_len = min(l1, l2, l3)
    differences = 0
    
    for i in range(min_len):
        c1, c2, c3 = sequences[0][i], sequences[1][i], sequences[2][i]
        
        if c1 != c2 or c2 != c3:
            if differences < 10:
                print(f"\n  -> Difference at Write #{i}:")
                print(f"       Pcap 1: Type {c1[0]}, Req {c1[1]}, wVal {c1[2]}, wIdx {c1[3]}, Data {c1[4]}")
                print(f"       Pcap 2: Type {c2[0]}, Req {c2[1]}, wVal {c2[2]}, wIdx {c2[3]}, Data {c2[4]}")
                print(f"       Pcap 3: Type {c3[0]}, Req {c3[1]}, wVal {c3[2]}, wIdx {c3[3]}, Data {c3[4]}")
            differences += 1
            
    print(f"\n[*] Total divergent commands found: {differences}")
    
    # Let's also hunt for non-0x40 commands just to see what we missed
    non_0x40 = [cmd for cmd in sequences[0] if cmd[0] != "0x40" or cmd[1] != "5"]
    print(f"[*] Found {len(non_0x40)} commands that WERE NOT '0x40 Write Register'.")
    if len(non_0x40) > 0 and len(non_0x40) < 20:
        for cmd in non_0x40:
            print(f"    - Type {cmd[0]}, Req {cmd[1]}, wVal {cmd[2]}, wIdx {cmd[3]}, Data {cmd[4]}")

if __name__ == "__main__":
    main()