import subprocess
import json
import sys
from pathlib import Path
import re

def extract_and_compact():
    pcap_path = "usb_dumps/captures_rt2800usb_rt5572/capture-1.pcap"
    log_path = "usb_dumps/captures_rt2800usb_rt5572/capture-1_logs/main.log"
    
    commands = []
    pattern = re.compile(r'^\[(\d+\.\d+)\]\s+\[T=\d+\.\d+s\]\s+Running:.*set channel (\d+)')
    with open(log_path, 'r') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                commands.append({'epoch': float(match.group(1)), 'ch': int(match.group(2))})
    
    tshark_cmd = ["tshark", "-r", pcap_path, "-T", "fields", "-e", "frame.number", "-e", "frame.time_epoch"]
    res = subprocess.run(tshark_cmd, capture_output=True, text=True, check=True)
    frames = []
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            frames.append((int(parts[0]), float(parts[1])))

    all_seqs = {}
    for i, cmd in enumerate(commands):
        start_t = cmd['epoch']
        end_t = commands[i+1]['epoch'] if i+1 < len(commands) else float('inf')
        
        start_f = next(f[0] for f in frames if f[1] >= start_t)
        end_f = next((f[0]-1 for f in frames if f[1] >= end_t), frames[-1][0])
        
        if end_f <= start_f: continue

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
        
        if seq:
            all_seqs[cmd['ch']] = seq
            print(f"Extracted {len(seq)} registers for Channel {cmd['ch']}")

    # Compaction
    channels = sorted(list(all_seqs.keys()))
    base_2g = all_seqs[1]
    first_5g = next(ch for ch in channels if ch >= 36)
    base_5g = all_seqs[first_5g]

    output_path = "src/wifit3/chips/rt5572/assets/rt5572_tuning.py"
    Path("src/wifit3/chips/rt5572/assets").mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        f.write("# RT5572 Tuning Sequences (Super-Compacted, Hex Strings)\n\n")
        
        def write_base(name, seq):
            f.write(f"{name} = [\n")
            for idx, val in seq:
                f.write(f"    ({hex(idx)}, '{val}'),\n")
            f.write("]\n\n")

        write_base("BASE_2G", base_2g)
        write_base("BASE_5G", base_5g)

        f.write("CHANNELS = {\n")
        for ch in channels:
            ref_base = base_5g if ch >= 36 else base_2g
            seq = all_seqs[ch]
            
            deltas = []
            max_len = max(len(seq), len(ref_base))
            for i in range(max_len):
                if i >= len(ref_base):
                    deltas.append((i, seq[i][0], seq[i][1]))
                elif i >= len(seq):
                    pass
                elif seq[i] != ref_base[i]:
                    deltas.append((i, seq[i][0], seq[i][1]))

            if not deltas:
                f.write(f"    {ch}: [],\n")
            else:
                f.write(f"    {ch}: [\n")
                for i, idx, val in deltas:
                    f.write(f"        ({i}, {hex(idx)}, '{val}'),\n")
                f.write("    ],\n")
        f.write("}\n\n")

        f.write("""
def get_sequence(channel):
    ch_key = int(channel)
    if ch_key not in CHANNELS:
        return None
    
    base = list(BASE_5G) if ch_key >= 36 else list(BASE_2G)
    
    for pos, idx, val in CHANNELS[ch_key]:
        if pos < len(base):
            base[pos] = (idx, val)
        else:
            base.append((idx, val))
            
    return base
""")

if __name__ == "__main__":
    extract_and_compact()
    print("Done")
