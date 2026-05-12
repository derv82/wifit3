import subprocess
import os

def extract_tuning(pcap_file, output_file):
    print(f"[*] Extracting Channel Tuning Sequences from {pcap_file}...")
    
    # Based on capture.py: 
    # T=20.0s to T=31.0s: 2.4GHz Hopping (Channels 1 to 12)
    # Each channel gets roughly 0.8 seconds before the next command.
    channels = {}
    for i, ch in enumerate(range(1, 13)):
        # Calculate the exact start and end bounds based on the capture script timeline.
        # e.g., Ch 1 starts at T=20.0, Ch 2 at T=21.0
        start_time = 20.0 + i - 0.2
        end_time = 20.0 + i + 0.5
        channels[ch] = (start_time, end_time)
        
    with open(output_file, "w") as f:
        f.write("# Auto-generated Hardware Channel Tuning Sequences\n\n")
        f.write("TUNING_SEQUENCES = {\n")
        
        for ch, (t_start, t_end) in channels.items():
            cmd = [
                "tshark.exe", "-r", pcap_file,
                f"-Y", f"frame.time_relative >= {t_start} and frame.time_relative <= {t_end} and (usb.bmRequestType == 0x40 or usb.bmRequestType == 0xc0)",
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

                if bmReq == "0x40" and bReq == "5" and payload:
                    raw_commands.append((float(t_str), "WRITE", wVal, wIdx, payload))
                elif bmReq == "0xc0" and bReq == "5":
                    raw_commands.append((float(t_str), "READ", wVal, wIdx, wLen))

            print(f"[*] Found {len(raw_commands)} instructions for Channel {ch}.")
            
            f.write(f"    {ch}: [\n")
            for i in range(len(raw_commands)):
                t_current, cmd_type, w_val, w_idx, data = raw_commands[i]
                
                if i < len(raw_commands) - 1:
                    t_next = raw_commands[i+1][0]
                    delay = max(0.0, t_next - t_current)
                    delay = min(delay, 0.200) # Cap at 200ms
                else:
                    delay = 0.010
                    
                if cmd_type == "WRITE":
                    byte_list = [f"0x{data[i:i+2]}" for i in range(0, len(data), 2)]
                    f.write(f"        ('{cmd_type}', {w_val}, {w_idx}, [{', '.join(byte_list)}], {delay:.4f}),\n")
                elif cmd_type == "READ":
                    f.write(f"        ('{cmd_type}', {w_val}, {w_idx}, {data}, {delay:.4f}),\n")
            f.write("    ],\n")
            
        f.write("}\n")
    print(f"[+] Done! Tuning sequences saved to {output_file}")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    pcap_file = os.path.normpath(os.path.join(base_dir, "../../../../../usb_dumps/captures_rtl8187/capture-1.pcap"))
    output_file = os.path.join(base_dir, "tuning.py")
    extract_tuning(pcap_file, output_file)
