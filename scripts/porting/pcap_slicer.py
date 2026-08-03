import argparse
import bisect
import re
import subprocess

def parse_log(log_path):
    """Extract (epoch, label) phase boundaries from a capture main.log.

    Recognises every phase capture.py emits: the plug-in marker, `Running:`
    commands (airmon/iw/aireplay), the airodump 250 ms-hop segment
    (start/stop), and each 0.25 s fast-hop. The epoch used is always the FIRST
    `[<epoch>]` on the line (log_main's own timestamp = when the event fired);
    the airodump/fast-hop lines carry a second inline epoch which we ignore.
    """
    commands = []
    epoch_re = re.compile(r'^\[(\d+\.\d+)\]')
    running_re = re.compile(r'Running:\s+(.*)')
    fasthop_re = re.compile(r'FAST-HOP set channel (\S+)')
    try:
        with open(log_path, 'r') as f:
            for line in f:
                m = epoch_re.search(line)
                if not m:
                    continue
                epoch = float(m.group(1))
                rest = line[m.end():]

                if 'INSERT THE USB CARD NOW' in line:
                    label = '<hardware_plugin_and_initialization>'
                elif 'Running:' in rest:
                    label = running_re.search(rest).group(1).strip()
                elif '[AIRODUMP] start' in rest:
                    label = '<AIRODUMP --band abg, native 250ms hop START>'
                elif '[AIRODUMP] stopped' in rest:
                    label = '<AIRODUMP STOP>'
                elif 'FAST-HOP set channel' in rest:
                    label = f'fast-hop 0.25s -> ch {fasthop_re.search(rest).group(1)}'
                else:
                    continue
                commands.append({'epoch': epoch, 'cmd': label})
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

    frame_nums = []
    frame_epochs = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            try:
                frame_nums.append(int(parts[0]))
                frame_epochs.append(float(parts[1]))
            except ValueError:
                pass

    if not frame_epochs:
        print("No frames parsed from pcap.")
        return

    n = len(frame_epochs)
    print(f"{'Phase':<52} | {'Start Epoch':<15} | {'Start Frame':<12} | {'End Frame':<10} | {'Frames':<8}")
    print("-" * 108)

    # frame_epochs is monotonically increasing (capture order) -> bisect is exact
    # and fast on the big (25-32 MB) usbmon pcaps. The "Frames" count per phase is
    # the signal we care about: a hop with ~0 frames is the RX-death fingerprint.
    for i, cmd_info in enumerate(commands):
        start_epoch = cmd_info['epoch']
        end_epoch = commands[i + 1]['epoch'] if i + 1 < len(commands) else float('inf')

        start_idx = bisect.bisect_left(frame_epochs, start_epoch)
        end_idx = bisect.bisect_left(frame_epochs, end_epoch) - 1  # last frame before next phase

        if start_idx >= n:
            start_frame, end_frame, count = "N/A", "N/A", 0
        else:
            end_idx = min(end_idx, n - 1)
            start_frame = frame_nums[start_idx]
            end_frame = frame_nums[end_idx]
            count = max(0, end_idx - start_idx + 1)

        print(f"{cmd_info['cmd']:<52} | {start_epoch:<15.3f} | "
              f"{str(start_frame):<12} | {str(end_frame):<10} | {count:<8}")

def main():
    parser = argparse.ArgumentParser(description="Map python capture logs to PCAP frames based on absolute Epoch time.")
    parser.add_argument("log_path", help="Path to the 'main' capture log (usb_data/captures_<chip>/capture-1_logs/main.log)")
    parser.add_argument("pcap_path", help="Path to the corresponding .pcap file (usb_data/captures_<chip>/capture-1.pcap)")
    
    args = parser.parse_args()
    
    commands = parse_log(args.log_path)
    if not commands:
        print("No 'Running:' command timestamps found in log.")
        return
        
    slice_pcap(args.pcap_path, commands)

if __name__ == '__main__':
    main()
