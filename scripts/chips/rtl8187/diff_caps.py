import subprocess
import os

# Update these paths if your files are named differently
PCAPS = [
    r"driver_captures\round2\awus036h_1.pcap",
    r"driver_captures\round2\awus036h_2.pcap",
    r"driver_captures\round2\awus036h_3.pcap"
]

def extract_writes(pcap_file):
    cmd = [
        "tshark.exe", "-r", pcap_file,
        "-Y", "usb.bmRequestType == 0x40 and usb.setup.bRequest == 5",
        "-T", "fields",
        "-e", "usb.setup.wValue",
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
        if len(parts) < 2: continue
        
        reg = parts[0]
        # Handle payload location variability
        payload = parts[1] if len(parts) > 1 and parts[1] else (parts[2] if len(parts) > 2 else "")
        payload = payload.replace(':', '').lower()
        
        if reg and payload:
            sequence.append((reg, payload))
            
            # The Golden Trigger: Stop collecting once airodump floodgates open
            if reg == "0xff40" and "ea98" in payload:
                break
                
    return sequence

def main():
    print("[*] Starting Differential Analysis on pcaps...\n")
    sequences = []
    
    for pcap in PCAPS:
        if not os.path.exists(pcap):
            print(f"[-] Missing {pcap}, please check the path.")
            return
            
        seq = extract_writes(pcap)
        print(f"[+] {pcap}: {len(seq)} writes extracted.")
        sequences.append(seq)
        
    # 1. Compare Lengths
    l1, l2, l3 = len(sequences[0]), len(sequences[1]), len(sequences[2])
    if l1 == l2 == l3:
        print("\n[+] Length Check: PASSED. All 3 pcaps have the exact same number of writes.")
    else:
        print(f"\n[-] Length Check: FAILED. Lengths differ ({l1} vs {l2} vs {l3}). The init logic branches.")
        
    # 2. Compare Bytes
    print("[*] Performing byte-by-byte comparison...")
    min_len = min(l1, l2, l3)
    differences = 0
    
    for i in range(min_len):
        c1, c2, c3 = sequences[0][i], sequences[1][i], sequences[2][i]
        
        # If any of the three differ
        if c1 != c2 or c2 != c3:
            if differences < 5: # Print only the first 5 to avoid spam
                print(f"  -> Difference at Write #{i}:")
                print(f"       Pcap 1: Reg {c1[0]}, Payload {c1[1]}")
                print(f"       Pcap 2: Reg {c2[0]}, Payload {c2[1]}")
                print(f"       Pcap 3: Reg {c3[0]}, Payload {c3[1]}")
            differences += 1
            
    print(f"\n[*] Total divergent commands found: {differences}")
    
    if differences == 0:
        print("[*] CONCLUSION: The boot sequence is 100% STATIC.")
        print("    The hardware is failing because we are missing USB Read (0xc0) polling delays.")
    else:
        print("[*] CONCLUSION: The boot sequence contains DYNAMIC variables.")
        print("    We must mask these specific bytes in our MVP script.")

if __name__ == "__main__":
    main()