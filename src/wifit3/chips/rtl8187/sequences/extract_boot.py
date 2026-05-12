import subprocess
import os

def extract_ultimate_bootstrap(pcap_file, output_file):
    print(f"[*] Extracting full Interleaved Sequence (Reads + Writes) from {pcap_file}...")
    
    cmd = [
        "tshark.exe", "-r", pcap_file,
        "-Y", "frame.number <= 16400 and (usb.bmRequestType == 0x40 or usb.bmRequestType == 0xc0 or usb.bmRequestType == 0x00)",
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

    # Pairing ACK Logic
    processed_commands = []
    i = 0
    while i < len(raw_commands):
        t, cmd, w, idx, data = raw_commands[i]
        
        if cmd == "WRITE" and i + 1 < len(raw_commands):
            t_next, next_cmd, next_w, next_idx, next_data = raw_commands[i+1]
            if next_cmd == "READ" and next_w == w:
                processed_commands.append((t, "WRITE_AND_WAIT", w, idx, data))
                i += 2
                continue
                
        processed_commands.append((t, cmd, w, idx, data))
        i += 1

    print(f"[*] Found {len(processed_commands)} instructions.")
    
    writes_with_delay = []
    for i in range(len(processed_commands)):
        t_current, cmd_type, w_val, w_idx, data = processed_commands[i]
        
        if i < len(processed_commands) - 1:
            t_next = processed_commands[i+1][0]
            delay = t_next - t_current
            
            # If the delay is very small (< 5ms), it's likely just host/bus latency.
            # We set these to 0.0 to execute as fast as the host can send them.
            if delay < 0.005:
                delay = 0.0
            else:
                delay = min(delay, 0.200)
        else:
            delay = 0.010
            
        writes_with_delay.append((cmd_type, w_val, w_idx, data, delay))

    with open(output_file, "w") as f:
        f.write("# Auto-generated Interleaved Hardware Bootstrap\n\n")
        f.write("FULL_BOOT_SEQUENCE = [\n")
        for c_type, w, idx, data, d in writes_with_delay:
            if c_type == "WRITE" or c_type == "WRITE_AND_WAIT":
                byte_list = [f"0x{data[j:j+2]}" for j in range(0, len(data), 2)]
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
