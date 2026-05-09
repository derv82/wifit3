import subprocess
import time
import re
import sys
import shutil
import tempfile
import os
from pathlib import Path

# ==============================================================================
# Wifit3 High-Precision Automated Capture Engine (Python)
# ==============================================================================

class LogHelper:
    def __init__(self, temp_dir_path):
        self.temp_dir = temp_dir_path
        
    def _timestamp(self):
        return f"{time.time():.3f}"
        
    def log_main(self, msg):
        log_file = self.temp_dir / "main.log"
        with open(log_file, "a") as f:
            f.write(f"[{self._timestamp()}] {msg}\n")
        print(msg)
            
    def log_cmd(self, cmd_list, stdout_text, return_code, elapsed_time):
        tool_name = cmd_list[0]
        if tool_name == "sudo" and len(cmd_list) > 1:
            tool_name = cmd_list[1]
            
        log_file = self.temp_dir / f"{tool_name}.log"
        cmd_string = " ".join(cmd_list)
        
        with open(log_file, "a") as f:
            f.write(f"[{self._timestamp()}] --------------\n")
            f.write(f"[{self._timestamp()}] Executing: {cmd_string}\n")
            if stdout_text:
                f.write(f"{stdout_text}")
                if not stdout_text.endswith('\n'):
                    f.write("\n")
            f.write(f"[{self._timestamp()}] Execution completed in {elapsed_time:.3f}s, return code: {return_code}\n")
            f.write("[ ] --------------\n")


class Capture:
    TARGET_BSSID = "aa:bb:cc:dd:ee:01"
    USBMON = "usbmon3"
    BASE_IFACE = "wlan1"
    
    def __init__(self):
        self.start_time = 0.0
        self.current_offset = 0.0
        self.mon_iface = None
        self.chipset = "unknown"
        self.tshark_proc = None
        
        self.temp_dir_obj = tempfile.TemporaryDirectory(prefix="wifit3_cap_")
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.logger = LogHelper(self.temp_dir)
        
    def throw(self, msg):
        self.logger.log_main(f"\n[ERROR] {msg}")
        self.cleanup()
        sys.exit(1)
        
    def wait_step(self, duration):
        self.current_offset += duration
        
    def run_seq(self, cmd_list, allocated_time):
        expected_start = self.start_time + self.current_offset
        now = time.time()
        
        if now > expected_start + 0.1:
            self.throw(f"TIMELINE CORRUPTION: Missed scheduled start for '{cmd_list[0]}' by {now - expected_start:.3f}s")
            
        if now < expected_start:
            time.sleep(expected_start - now)
            
        self.logger.log_main(f"[T={self.current_offset:05.2f}s] Running: {' '.join(cmd_list)}")
        
        start_exec = time.time()
        try:
            res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=allocated_time)
        except subprocess.TimeoutExpired:
            self.throw(f"TIMEOUT EXPIRED: Command '{' '.join(cmd_list)}' hung longer than its {allocated_time}s allocated window.")
            
        elapsed = time.time() - start_exec
        combined_output = (res.stdout or "") + (res.stderr or "")
        self.logger.log_cmd(cmd_list, combined_output, res.returncode, elapsed)
        
        self.current_offset += allocated_time
        return combined_output
        
    def cleanup(self):
        self.logger.log_main("\n[*] Teardown initiated. Cleaning up...")
        if self.tshark_proc and self.tshark_proc.poll() is None:
            time.sleep(1) # flush
            subprocess.run(["sudo", "pkill", "-P", str(self.tshark_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "kill", str(self.tshark_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.tshark_proc.wait()
            
        if self.mon_iface:
            subprocess.run(["sudo", "airmon-ng", "stop", self.mon_iface], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["sudo", "airmon-ng", "stop", f"{self.BASE_IFACE}mon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "airmon-ng", "stop", self.BASE_IFACE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        dest_dir = Path(__file__).parent / f"captures_{self.chipset}"
        dest_dir.mkdir(exist_ok=True)
        
        count = 1
        while (dest_dir / f"capture-{count}.pcap").exists():
            count += 1
            
        final_pcap = dest_dir / f"capture-{count}.pcap"
        tmp_pcap = self.temp_dir / "capture.pcap"
        
        if tmp_pcap.exists():
            subprocess.run(["sudo", "mv", str(tmp_pcap), str(final_pcap)])
            user = os.environ.get("USER", "root")
            subprocess.run(["sudo", "chown", f"{user}:{user}", str(final_pcap)])
            self.logger.log_main(f"[+] Saved capture to: {final_pcap}")
            
        self.logger.log_main("[+] Cleanup complete. Safe to unplug.")
        
        final_logs = dest_dir / f"capture-{count}_logs"
        final_logs.mkdir(exist_ok=True)
        for log_file in self.temp_dir.glob("*.log"):
            shutil.copy(log_file, final_logs)
            
        self.temp_dir_obj.cleanup()

    def run(self):
        self.logger.log_main("--- Wifit3 Automated Capture Tool ---")
        subprocess.run(["sudo", "airmon-ng", "check", "kill"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        self.logger.log_main("\n[!] PREPARATION: Prepare to plug in the USB card.")
        self.logger.log_main("[!] IMPORTANT: DO NOT plug it in yet.")
        input("\nPress ENTER to start the capture sequence...")
        
        self.start_time = time.time()
        self.logger.log_main("\n[T=00.00s] STARTING TIMELINE")
        
        # T=0
        pcap_path = self.temp_dir / "capture.pcap"
        self.tshark_proc = subprocess.Popen(["sudo", "tshark", "-i", self.USBMON, "-w", str(pcap_path), "-q"])
        self.logger.log_main(f"[{time.time():.3f}] --> INSERT THE USB CARD NOW <--")
        self.wait_step(10.0)
        
        # T=10
        stdout = self.run_seq(["sudo", "airmon-ng", "start", self.BASE_IFACE], 10.0)
        
        lines = stdout.split('\n')
        target_phy = None
        for line in lines:
            if self.BASE_IFACE in line and "phy" in line:
                match = re.search(r'(phy\d+)', line)
                if match:
                    target_phy = match.group(1)
                    break
                    
        if not target_phy:
            self.throw(f"Failed to detect target phy# for {self.BASE_IFACE} in airmon-ng output. Expected a line containing 'phy' and '{self.BASE_IFACE}'.\nSTDOUT:\n{stdout}")
            
        match = re.search(rf'monitor mode vif enabled for \[{target_phy}\]{self.BASE_IFACE} on \[{target_phy}\](\w+)', stdout)
        if match:
            self.mon_iface = match.group(1)
        else:
            self.logger.log_main(f"[!] Warning: Strict regex failed to match monitor interface. Falling back to guess.")
            self.mon_iface = f"{self.BASE_IFACE}mon"
            
        for line in lines:
            if target_phy in line and self.BASE_IFACE in line:
                parts = line.split()
                if len(parts) >= 3:
                    self.chipset = parts[2].replace(",", "")
                break
                
        self.logger.log_main(f"[*] Detected Monitor Interface: {self.mon_iface}")
        self.logger.log_main(f"[*] Detected Chipset/Driver:  {self.chipset}")
        
        # T=20 to T=31
        for ch in range(1, 13):
            self.run_seq(["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)], 1.0)
            
        # T=32
        self.wait_step(2.0)
        
        # T=34
        self.run_seq(["sudo", "iw", "dev", self.mon_iface, "set", "channel", "1"], 2.0)
        
        # T=36
        self.run_seq(["sudo", "aireplay-ng", "-a", self.TARGET_BSSID, "--test", self.mon_iface], 5.0)
        
        # T=41
        self.cleanup()

if __name__ == "__main__":
    try:
        app = Capture()
        app.run()
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C caught. Exiting.")
        if 'app' in locals():
            app.cleanup()
        sys.exit(1)
