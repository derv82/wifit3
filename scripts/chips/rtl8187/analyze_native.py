import subprocess
import sys

def analyze_pcap(pcap_file):
    print(f"\n[*] Analyzing {pcap_file} with native tshark...")
    
    # We ask tshark to print exactly the fields we need, separated by pipes (|)
    cmd = [
        "tshark.exe", "-r", pcap_file,
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time_relative",
        "-e", "usb.idVendor",
        "-e", "usb.setup.wValue",
        "-e", "usb.endpoint_address",
        "-e", "usb.data_fragment",
        "-e", "usb.capdata",
        "-E", "separator=|"
    ]
    
    try:
        # Run tshark in the background and stream the output
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("[-] Error: tshark.exe not found. Make sure Wireshark is in your PATH.")
        return

    state = "STEP_1_PLUGIN"
    last_hop_time = 0.0
    injections = 0
    
    for line in process.stdout:
        # Split the piped fields
        parts = line.strip('\n').split('|')
        if len(parts) < 7: continue
        
        frame_num, rel_time, id_vendor, w_value, endpoint, data_frag, capdata = parts
        
        # Combine possible payload fields and normalize to lowercase without colons
        payload = (data_frag + capdata).lower().replace(':', '')
        
        try:
            t = float(rel_time)
        except ValueError:
            continue

        # -----------------------------------------
        # 1. Plugin (Vendor ID 0x0bda)
        # -----------------------------------------
        if state == "STEP_1_PLUGIN" and 'bda' in id_vendor.lower():
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 1: AWUS036H Plugged In")
            state = "STEP_2_MONITOR"
            continue
            
        # -----------------------------------------
        # 2. Monitor Mode (wValue 0xff44, payload 0bfc9c90)
        # -----------------------------------------
        if state == "STEP_2_MONITOR" and 'ff44' in w_value.lower() and '0bfc9c90' in payload:
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 2: Monitor Mode Enabled (0xff44 written)")
            state = "STEP_3_CH6"
            continue
            
        # -----------------------------------------
        # 3. Channel 6 Hop (wValue 0xff7d)
        # -----------------------------------------
        if state == "STEP_3_CH6" and 'ff7d' in w_value.lower():
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 3: Baseband Tune (Channel 6)")
            last_hop_time = t
            state = "STEP_4_CH1"
            continue
            
        # -----------------------------------------
        # 4. Channel 1 Hop (Wait > 3s to avoid catching the rest of the Ch6 loop)
        # -----------------------------------------
        if state == "STEP_4_CH1" and 'ff7d' in w_value.lower() and (t - last_hop_time) > 3.0:
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 4: Baseband Tune (Channel 1)")
            state = "STEP_5_AIRODUMP"
            continue
            
        # -----------------------------------------
        # 5a. Airodump-ng starts (Bulk IN traffic on endpoint 0x81)
        # -----------------------------------------
        if state == "STEP_5_AIRODUMP" and '81' in endpoint:
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 5a: Sniffing Started (Bulk IN 0x81 flowing)")
            state = "STEP_5_AIREPLAY"
            continue
            
        # -----------------------------------------
        # 5b. Aireplay-ng starts (Bulk OUT 0x02 to target MAC)
        # -----------------------------------------
        if state in ["STEP_5_AIREPLAY", "STEP_6_STOP"] and '02' in endpoint and 'aa:bb:cc:dd:ee:01' in payload:
            if state == "STEP_5_AIREPLAY":
                print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 5b: Injection Started (Bulk OUT to Target AP)")
                state = "STEP_6_STOP"
            injections += 1
            
        # -----------------------------------------
        # 6. Stop Monitor Mode (0xff44 written without the promisc bitmask)
        # -----------------------------------------
        if state == "STEP_6_STOP" and 'ff44' in w_value.lower() and '0bfc9c90' not in payload:
            print(f"[{t:>6.2f}s] (Frame {frame_num}) [+] Step 6: Monitor Mode Stopped (RCR Reset)")
            print(f"          -> Total packets injected to target AP: {injections}")
            state = "DONE"
            break

    if state != "DONE":
        print(f"[*] Finished reading file. Ended on state: {state}")

if __name__ == "__main__":
    analyze_pcap(r"driver_captures\round2\awus036h_1.pcap")