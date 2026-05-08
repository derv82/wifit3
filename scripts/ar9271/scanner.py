import time
import sys
import os

# Ensure we can import the generated template from the root dir
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from ar9271_golden_template import get_channel_hop_template
except ImportError:
    print("[-] Could not import golden template. Please generate it first.")
    sys.exit(1)

class AR9271Scanner:
    """
    Orchestrates the channel hopping and parsing of 802.11 frames.
    """
    def __init__(self, usb_manager):
        self.usb = usb_manager
        
    def hop_channel(self, channel):
        print(f"[*] Executing Golden Sequence for Channel {channel}...")
        sequence = get_channel_hop_template(target_channel=channel)
        
        success_count = 0
        for i, cmd in enumerate(sequence):
            sys.stdout.write(f"\r    -> Firing: [ {i+1} / {len(sequence)} ] ")
            sys.stdout.flush()
            
            # Send the command and wait for the hardware to ACK it
            if self.usb.send_wmi_command(cmd, wait_for_ack=True):
                success_count += 1
            
        print(f"\n[+] Tuned AR9271 to Channel {channel} ({success_count}/{len(sequence)} ACKs).")

    def format_mac(self, mac_bytes):
        return ':'.join(f'{b:02x}' for b in mac_bytes)

    def process_rx_queue(self, timeout=0.1):
        """
        Pulls frames from the RX queue and prints them.
        Returns the number of frames processed.
        """
        import queue
        count = 0
        while True:
            try:
                frame_data, rssi, ts = self.usb.rx_queue.get(timeout=timeout)
                count += 1
                
                fc = frame_data[0]
                frame_type = (fc >> 2) & 0x03
                frame_subtype = (fc >> 4) & 0x0f

                types = {0: "Mgt", 1: "Ctrl", 2: "Data"}
                type_str = types.get(frame_type, f"Type {frame_type}")
                sub_str = f"Sub {frame_subtype}"

                if frame_type in [0, 2]:
                    if frame_type == 0 and frame_subtype == 8: sub_str = "Beacon"
                    elif frame_type == 0 and frame_subtype == 4: sub_str = "ProbeReq"
                    elif frame_type == 0 and frame_subtype == 5: sub_str = "ProbeResp"
                    elif frame_type == 0 and frame_subtype == 12: sub_str = "Deauth"
                    elif frame_type == 2: sub_str = "Data"
                    
                    addr1 = self.format_mac(frame_data[4:10])  # DA
                    addr2 = self.format_mac(frame_data[10:16]) # SA
                    
                    ssid = ""
                    if frame_type == 0 and frame_subtype in [4, 5, 8]:
                        element_offset = 36 if frame_subtype in [5, 8] else 24
                        while element_offset < len(frame_data) - 1:
                            tag_num = frame_data[element_offset]
                            tag_len = frame_data[element_offset+1]
                            if tag_num == 0: 
                                ssid_bytes = frame_data[element_offset+2 : element_offset+2+tag_len]
                                ssid = ssid_bytes.decode('utf-8', errors='ignore')
                                break
                            element_offset += 2 + tag_len
                    
                    proof = ""
                    my_macs = ["00:c0:ca:96:fe:3d", "fa:e4:4e:22:b7:76", "ff:ff:ff:ff:ff:ff"]
                    if addr1 not in my_macs:
                        proof = "  [🎯 PROOF OF MONITOR MODE]"

                    ssid_display = f" | SSID: '{ssid}'" if ssid else ""
                    print(f"    -> [RSSI: {rssi:3d}] 802.11 {type_str:<4} ({sub_str:<8}) | DA: {addr1} | SA: {addr2}{ssid_display}{proof}")
                    
            except queue.Empty:
                break
                
        return count
