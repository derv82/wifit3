import subprocess
import os
import re

def extract_tuning(pcap_file, output_file, iw_log_file):
    print(f"[*] Extracting Channel Tuning Sequences from {pcap_file}...")
    
    with open(iw_log_file, "r") as f:
        log_lines = f.readlines()
        
    channels = {}
    current_ch = None
    
    for line in log_lines:
        if "Executing: sudo iw dev wlan1 set channel " in line:
            m = re.search(r"set channel (\d+)", line)
            if m:
                current_ch = int(m.group(1))
                
        elif "Execution completed in" in line and current_ch:
            m_time = re.search(r"\[([\d\.]+)\]", line)
            m_dur = re.search(r"completed in ([\d\.]+)s", line)
            if m_time and m_dur:
                end_time = float(m_time.group(1))
                duration = float(m_dur.group(1))
                
                # The timestamp in the log is the END time of the execution.
                # Start time is end_time - duration.
                start_time = end_time - duration
                
                # Add a safe buffer (100ms on each side) to capture all USB traffic for this command
                # Only save the first time we see a channel (since Ch 1 is visited twice)
                if current_ch not in channels:
                    channels[current_ch] = (start_time - 0.1, end_time + 0.1)
                    
            current_ch = None
            
    with open(output_file, "w") as f:
        f.write("# Auto-generated Hardware Channel Tuning Sequences\n\n")
        f.write("TUNING_SEQUENCES = {\n")
        
        for ch, (t_start, t_end) in channels.items():
            if ch > 14: continue # Ignore 5GHz channels
            
            cmd = [
                "tshark.exe", "-r", pcap_file,
                f"-Y", f"frame.time_epoch >= {t_start} and frame.time_epoch <= {t_end} and (usb.bmRequestType == 0x40 or usb.bmRequestType == 0xc0)",
                "-T", "fields",
                "-e", "frame.time_epoch",
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

            print(f"[*] Found {len(raw_commands)} instructions for Channel {ch} (T_epoch={t_start:.3f} to {t_end:.3f}).")
            
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
    iw_log_file = os.path.normpath(os.path.join(base_dir, "../../../../../usb_dumps/captures_rtl8187/capture-1_logs/iw.log"))
    output_file = os.path.join(base_dir, "tuning.py")
    extract_tuning(pcap_file, output_file, iw_log_file)