import subprocess
import os

def extract_ultimate_bootstrap(pcap_file, output_file):
    print(f"[*] Extracting full Interleaved Sequence (Reads + Writes) from {pcap_file}...")
    
    cmd = [
        "tshark.exe", "-r", pcap_file,
        # We capture up to T=18.0s (airmon-ng completes before T=18s, ch1 tune starts T=20s)
        "-Y", "frame.time_relative <= 18.0 and (usb.bmRequestType == 0x40 or usb.bmRequestType == 0xc0 or usb.bmRequestType == 0x00)",
        "-T", "fields",
        "-e", "frame.time_relative",
        "-e", "usb.bmRequestType",
        "-e", "usb.setup.bRequest",
        "-e", "usb.setup.wValue",
        "-e", "usb.setup.wIndex",
        "-e", "usb.setup.wLength",
        "-e", "usb.data_fragment",
        "-e", "usb.capdata",
        "-E", "separator=|"
    ]
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("[-] tshark.exe not found.")
        return

    raw_commands = []
    for line in process.stdout:
        parts = line.strip('\n').split('|')
        if len(parts) < 6: continue
        
        t_str = parts[0]
        bmReq = parts[1]
        bReq = parts[2]
        wVal = parts[3]
        wIdx = parts[4] if parts[4] else "0"
        wLen = parts[5] if parts[5] else "0"
        
        payload = parts[6] if len(parts) > 6 and parts[6] else (parts[7] if len(parts) > 7 else "")
        payload = payload.replace(':', '')
        
        if not t_str or not bmReq: continue

        # VENDOR WRITE
        if bmReq == "0x40" and bReq == "5" and payload:
            raw_commands.append((float(t_str), "WRITE", wVal, wIdx, payload))
        # VENDOR READ
        elif bmReq == "0xc0" and bReq == "5":
            raw_commands.append((float(t_str), "READ", wVal, wIdx, wLen))
        # STANDARD SET CONFIG
        elif bmReq == "0x00" and bReq == "9":
            raw_commands.append((float(t_str), "SET_CONFIG", "0", "0", "0"))

    print(f"[*] Found {len(raw_commands)} instructions. Calculating EXACT micro-delays...")
    
    writes_with_delay = []
    for i in range(len(raw_commands)):
        t_current, cmd_type, w_val, w_idx, data = raw_commands[i]
        
        if i < len(raw_commands) - 1:
            t_next = raw_commands[i+1][0]
            delay = max(0.0, t_next - t_current) # EXACT DELAY
            
            # The Linux driver uses msleep(200) as its maximum hardware wait.
            # Any delay > 200ms in the PCAP is an artifact of host CPU lag (e.g. airmon-ng running).
            # We cap it to 0.2s to prevent insane wall-clock lags during boot.
            delay = min(delay, 0.200)
        else:
            delay = 0.010
            
        writes_with_delay.append((cmd_type, w_val, w_idx, data, delay))

    with open(output_file, "w") as f:
        f.write("# Auto-generated Interleaved Hardware Bootstrap\n\n")
        f.write("FULL_BOOT_SEQUENCE = [\n")
        for c_type, w, idx, data, d in writes_with_delay:
            if c_type == "WRITE":
                byte_list = [f"0x{data[i:i+2]}" for i in range(0, len(data), 2)]
                f.write(f"    ('{c_type}', {w}, {idx}, [{', '.join(byte_list)}], {d:.4f}),\n")
            elif c_type == "READ":
                f.write(f"    ('{c_type}', {w}, {idx}, {data}, {d:.4f}),\n")
            elif c_type == "SET_CONFIG":
                f.write(f"    ('{c_type}', 0, 0, 0, {d:.4f}),\n")
        f.write("]\n")

    print(f"[+] Done! Interleaved sequence saved to {output_file}")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    pcap_file = os.path.normpath(os.path.join(base_dir, "../../../../../usb_dumps/captures_rtl8187/capture-1.pcap"))
    output_file = os.path.join(base_dir, "init.py")
    extract_ultimate_bootstrap(pcap_file, output_file)
