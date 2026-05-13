import subprocess
import sys
from pathlib import Path

def extract_init_seq(pcap_path):
    start_f = 750
    end_f = 6652
    filter_str = f"frame.number >= {start_f} and frame.number <= {end_f} and usb.setup.bRequest == 6 and usb.bmRequestType == 0x40"
    extract_cmd = ["tshark", "-r", pcap_path, "-Y", filter_str, "-T", "fields", "-e", "usb.setup.wIndex", "-e", "usb.data_fragment"]
    
    reg_res = subprocess.run(extract_cmd, capture_output=True, text=True, check=True)
    
    seq = []
    for line in reg_res.stdout.splitlines():
        parts = line.strip().split('\t')
        if len(parts) == 2:
            idx_dec = int(parts[0])
            data_hex = parts[1].replace(':', '')
            seq.append((idx_dec, data_hex))
            
    print(f"Extracted {len(seq)} registers for Init Sequence")
    
    output_path = "src/wifit3/chips/rt5572/assets/rt5572_init.py"
    Path("src/wifit3/chips/rt5572/assets").mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        f.write("# RT5572 Initialization Sequence (Extracted from capture-1.pcap)\n\n")
        f.write("INIT_SEQ = [\n")
        for idx, val in seq:
            f.write(f"    ({hex(idx)}, '{val}'),\n")
        f.write("]\n")

if __name__ == "__main__":
    pcap = "usb_dumps/captures_rt2800usb_rt5572/capture-1.pcap"
    extract_init_seq(pcap)
    print("Done.")
