import argparse
import re
import subprocess
import sys

def parse_log(log_path):
    commands = []
    # Match lines like: [1778283068.474] [T=10.00s] Running: sudo airmon-ng start wlan1
    pattern = re.compile(r'^\[(\d+\.\d+)\]\s+\[T=\d+\.\d+s\]\s+Running:\s+(.*)')
    try:
        with open(log_path, 'r') as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    epoch = float(match.group(1))
                    cmd = match.group(2).strip()
                    commands.append({'epoch': epoch, 'cmd': cmd})
    except Exception as e:
        print(f"Error reading log: {e}")
    return commands

def slice_pcap(pcap_path, commands):
    print("Parsing PCAP with tshark... this may take a moment.")
    try:
        tshark_cmd = [
            "tshark", "-r", pcap_path, 
            "-T", "fields", 
            "-e", "frame.number", 
            "-e", "frame.time_epoch"
        ]
        result = subprocess.run(tshark_cmd, capture_output=True, text=True, check=True)
    except Exception as e:
        print(f"Error running tshark: {e}")
        return

    frames = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            try:
                frames.append((int(parts[0]), float(parts[1])))
            except ValueError:
                pass

    if not frames:
        print("No frames parsed from pcap.")
        return

    print(f"{'Command':<45} | {'Start Epoch':<15} | {'Start Frame':<12} | {'End Frame':<10}")
    print("-" * 90)

    for i, cmd_info in enumerate(commands):
        start_epoch = cmd_info['epoch']
        end_epoch = commands[i+1]['epoch'] if i + 1 < len(commands) else float('inf')
        
        start_frame = None
        end_frame = None
        
        for f_num, f_epoch in frames:
            if f_epoch >= start_epoch and start_frame is None:
                start_frame = f_num
            if f_epoch >= end_epoch and end_frame is None:
                # The command's effective range ends right before the next command's epoch
                end_frame = f_num - 1
                break
                
        if start_frame is None:
            start_frame = "N/A"
            end_frame = "N/A"
        elif end_frame is None:
            # If it's the last command, the end frame is the last frame in the pcap
            end_frame = frames[-1][0]
            
        print(f"{cmd_info['cmd']:<45} | {start_epoch:<15.3f} | {str(start_frame):<12} | {str(end_frame):<10}")

def main():
    parser = argparse.ArgumentParser(description="Map python capture logs to PCAP frames based on absolute Epoch time.")
    parser.add_argument("log_path", help="Path to the python capture log (e.g., main.log)")
    parser.add_argument("pcap_path", help="Path to the corresponding .pcap file")
    
    args = parser.parse_args()
    
    commands = parse_log(args.log_path)
    if not commands:
        print("No 'Running:' command timestamps found in log.")
        return
        
    slice_pcap(args.pcap_path, commands)

if __name__ == '__main__':
    main()
