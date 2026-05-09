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
# This script uses an ABSOLUTE timeline (target_t).
# If you schedule a command for target_t=20.0, it executes exactly 20 seconds
# after the timeline starts. No rolling offsets, no confusing math.
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
        # Use \r\n to prevent stair-stepping in terminals left in raw mode
        sys.stdout.write(f"{msg}\r\n")
        sys.stdout.flush()
            
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
        self.mon_iface = None
        self.chipset = "unknown"
        self.tshark_proc = None
        self.tshark_log = None
        
        self.temp_dir_obj = tempfile.TemporaryDirectory(prefix="wifit3_cap_")
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.logger = LogHelper(self.temp_dir)
        
    def throw(self, msg):
        self.logger.log_main(f"\n[ERROR] {msg}")
        self.cleanup()
        sys.exit(1)
        
    def run_at(self, target_t, cmd_list, timeout=2.0):
        """
        Sleeps until (start_time + target_t), then executes the command.
        target_t: Absolute seconds since T=0 (e.g., 20.0).
        timeout: Maximum seconds to wait for command completion.
        """
        expected_start = self.start_time + target_t
        now = time.time()
        
        # 1. Check for timeline drift
        if now > expected_start + 0.1:
            self.throw(f"TIMELINE DRIFT: '{cmd_list[0]}' scheduled for T={target_t:.1f}s, but it is already T={now - self.start_time:.1f}s")
            
        # 2. Wait until exact start time
        if now < expected_start:
            time.sleep(expected_start - now)
            
        self.logger.log_main(f"[T={target_t:05.2f}s] Running: {' '.join(cmd_list)}")
        
        # 3. Execute
        start_exec = time.time()
        try:
            res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.throw(f"TIMEOUT: Command '{' '.join(cmd_list)}' hung longer than {timeout}s.")
            
        # 4. Log
        elapsed = time.time() - start_exec
        combined_output = (res.stdout or "") + (res.stderr or "")
        self.logger.log_cmd(cmd_list, combined_output, res.returncode, elapsed)
        
        return combined_output
        
    def cleanup(self):
        self.logger.log_main("\n[*] Teardown initiated. Cleaning up...")
        if self.tshark_proc and self.tshark_proc.poll() is None:
            time.sleep(1) # flush
            subprocess.run(["sudo", "pkill", "-P", str(self.tshark_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "kill", str(self.tshark_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.tshark_proc.wait()
            
        if self.tshark_log:
            self.tshark_log.close()
            
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
            self.logger.log_main(f"[+] Saved capture to: {final_pcap}")
            
        self.logger.log_main("[+] Cleanup complete. Safe to unplug.")
        
        final_logs = dest_dir / f"capture-{count}_logs"
        final_logs.mkdir(exist_ok=True)
        for log_file in self.temp_dir.glob("*.log"):
            shutil.copy(log_file, final_logs)
            
        # Fix permissions for the entire destination directory
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            subprocess.run(["sudo", "chown", "-R", f"{sudo_user}:{sudo_user}", str(dest_dir)])
            
        # Reset terminal to prevent stair-stepping
        subprocess.run(["stty", "sane"], stderr=subprocess.DEVNULL)
            
        self.temp_dir_obj.cleanup()

    def run(self):
        self.logger.log_main("--- Wifit3 Automated Capture Tool ---")
        
        # 0. System Prep
        subprocess.run(["sudo", "modprobe", "usbmon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "airmon-ng", "check", "kill"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        self.logger.log_main("\n[!] PREPARATION: Prepare to plug in the USB card.")
        self.logger.log_main("[!] IMPORTANT: DO NOT plug it in yet.")
        input("\nPress ENTER to start the capture sequence...")
        
        self.start_time = time.time()
        self.logger.log_main("\n[T=00.00s] STARTING TIMELINE")
        
        # T=0
        pcap_path = self.temp_dir / "capture.pcap"
        self.tshark_log = open(self.temp_dir / "tshark.log", "w")
        self.tshark_proc = subprocess.Popen(
            ["sudo", "tshark", "-i", self.USBMON, "-w", str(pcap_path), "-q"],
            stdout=self.tshark_log, stderr=self.tshark_log
        )
        self.logger.log_main(f"[{time.time():.3f}] --> INSERT THE USB CARD NOW <--")
        
        # ======================================================================
        # THE TIMELINE (ABSOLUTE OFFSETS)
        # ======================================================================
        
        # T=10.0s: Start monitor mode
        stdout = self.run_at(10.0, ["sudo", "airmon-ng", "start", self.BASE_IFACE], timeout=8.0)
        
        # --- Interface & Chipset Parsing Logic ---
        iw_out = subprocess.run(["iw", "dev"], capture_output=True, text=True).stdout
        target_phy = None
        current_phy = None
        for line in iw_out.splitlines():
            if line.startswith("phy#") or line.startswith("phy"):
                current_phy = line.strip().split('#')[-1]
            if f"Interface {self.BASE_IFACE}" in line:
                target_phy = current_phy
                break
        
        self.mon_iface = None
        current_phy = None
        current_iface = None
        for line in iw_out.splitlines():
            if line.startswith("phy#") or line.startswith("phy"):
                current_phy = line.strip().split('#')[-1]
            if "Interface " in line:
                current_iface = line.split("Interface ")[1].strip()
            if "type monitor" in line and current_phy == target_phy:
                self.mon_iface = current_iface
                break
        
        if not self.mon_iface:
            self.logger.log_main(f"[!] Warning: Could not find monitor interface on {target_phy}. Falling back to {self.BASE_IFACE}.")
            self.mon_iface = self.BASE_IFACE
            
        airmon_check = subprocess.run(["airmon-ng"], capture_output=True, text=True).stdout
        for line in airmon_check.splitlines():
            if self.BASE_IFACE in line:
                parts = line.split()
                if len(parts) >= 3:
                    self.chipset = parts[2].replace(",", "").replace("/", "_")
                break
                
        self.logger.log_main(f"[*] Detected Monitor Interface: {self.mon_iface}")
        self.logger.log_main(f"[*] Detected Chipset/Driver:  {self.chipset}")
        # -----------------------------------------
        
        # T=20.0s to T=31.0s: 2.4GHz Hopping
        for i, ch in enumerate(range(1, 13)):
            self.run_at(20.0 + i, ["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)], timeout=0.8)
            
        # T=32.0s to T=35.0s: 5GHz Hopping
        for i, ch in enumerate([36, 44, 149, 157]):
            self.run_at(32.0 + i, ["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)], timeout=0.8)
            
        # T=36.0s: Return to Channel 1
        self.run_at(36.0, ["sudo", "iw", "dev", self.mon_iface, "set", "channel", "1"], timeout=1.8)
        
        # T=38.0s: Aireplay Test
        self.run_at(38.0, ["sudo", "aireplay-ng", "-a", self.TARGET_BSSID, "--test", self.mon_iface], timeout=6.0)
        
        # T=45.0s: Cleanup
        # We don't use run_at for cleanup because it doesn't run a subprocess, it just wraps up.
        # So we manually sleep until T=45.0s
        expected_cleanup = self.start_time + 45.0
        now = time.time()
        if now < expected_cleanup:
            time.sleep(expected_cleanup - now)
            
        self.cleanup()

if __name__ == "__main__":
    try:
        app = Capture()
        app.run()
    except KeyboardInterrupt:
        # Use stdout.write to ensure clean newline even in raw mode
        sys.stdout.write("\r\n[!] Ctrl+C caught. Exiting.\r\n")
        if 'app' in locals():
            app.cleanup()
        sys.exit(1)
