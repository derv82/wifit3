"""Automated USB capture tool for reverse-engineering a Wi-Fi adapter's driver.

Run on the (Linux) capture box as root, with the card UNPLUGGED:

    sudo python capture.py [--target <BSSID>] [--client <BSSID>] [--debug-segments]

It starts a full usbmon0 (all-buses) tshark capture, walks the card through
monitor-mode bring-up, a channel sweep, and an optional injection test, then
saves the pcap + per-tool logs + the bound driver's firmware (and source, if
on disk) under `captures_<chipset>/`. `main.log` records when each step ran
(epoch timestamps); `pcap_slicer.py` maps those to pcap frame ranges.

Options:
  --target BSSID    AP for the aireplay injection test + deauth. Omit and both
                    are skipped (the scan/monitor capture still happens).
  --client BSSID    client for the deauth (needs --target too).
  --debug-segments  also run the airodump + 0.25 s fast-hop investigation
                    segments (they add ~20 MB to the pcap).
"""

import argparse
import subprocess
import time
import sys
import shutil
import tempfile
import os
from pathlib import Path


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

    def log_cmd(self, cmd_list, stdout_text, return_code, start_exec, elapsed_time):
        tool_name = cmd_list[0]
        if tool_name == "sudo" and len(cmd_list) > 1:
            tool_name = cmd_list[1]

        log_file = self.temp_dir / f"{tool_name}.log"
        cmd_string = " ".join(cmd_list)

        with open(log_file, "a") as f:
            f.write("-----------------------------------\n")
            f.write(f"[{start_exec:.3f}] Executing: {cmd_string}\n")
            if stdout_text:
                f.write(f"{stdout_text}")
                if not stdout_text.endswith('\n'):
                    f.write("\n")
            f.write(f"[{time.time():.3f}] Execution completed in {elapsed_time:.3f}s, return code: {return_code}\n")
            f.write("-----------------------------------\n")


class Capture:
    # usbmon0 = the ALL-BUSES meta-interface. A hardcoded per-bus capture
    # (e.g. usbmon3) silently loses the device when a FW-loading adapter
    # USB-resets and re-enumerates onto a different bus after firmware boot —
    # which is why earlier captures stopped dead right after init (3 caps all
    # ended at ~5260 frames = the init burst, nothing after). Capturing all
    # buses is immune to the bus number and to re-enumeration; the extract
    # tooling filters by the vendor-control signature anyway.
    USBMON = "usbmon0"
    BASE_IFACE = "wlan1"
    # Seconds to wait after "INSERT CARD NOW" for the operator to plug in and the
    # device to enumerate, before bringing up monitor mode.
    PLUG_IN_WAIT = 10

    def __init__(self, target=None, client=None, debug_segments=False):
        self.target_bssid = target
        self.client_bssid = client
        # airodump + fast-hop are per-chip investigation segments; they add
        # ~20 MB to the pcap and aren't needed for a normal bring-up capture.
        self.debug_segments = debug_segments

        self.start_time = 0.0
        self.mon_iface = None
        self.chipset = "unknown"
        self.tshark_proc = None
        self.tshark_log = None
        self.airodump_proc = None
        self.airodump_log = None
        self.supports_5g = False
        self.lsusb_baseline = ""
        self.driver_module = None
        self.firmware_files = []

        # /dev/shm is always tmpfs (RAM): writing the pcap there means no
        # persistent-USB writes during capture, so a usbmon0 capture on a
        # persistent-USB box can't feed its own storage writes back into itself.
        # The pcap is moved to the capture dir at teardown, after tshark stops.
        shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
        self.temp_dir_obj = tempfile.TemporaryDirectory(prefix="wifit3_cap_", dir=shm)
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.logger = LogHelper(self.temp_dir)

    # --- Pure parsers (no I/O) — extracted so they're unit-testable without
    # hardware. Tests: tests/scripts/test_capture.py. ---

    @staticmethod
    def parse_chipset(airmon_text, base_iface):
        """Driver name for base_iface from `airmon-ng` table output (columns:
        PHY  Interface  Driver  Chipset). Returns the Driver column with ','/'/'
        folded to '_' (it becomes a dir name), or None if base_iface has no row
        — which means airmon-ng never bound the card."""
        for line in airmon_text.splitlines():
            if base_iface in line:
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2].replace(",", "").replace("/", "_")
                break
        return None

    @staticmethod
    def parse_monitor_iface(iw_text, base_iface):
        """From `iw dev`: the monitor-type interface sharing base_iface's phy
        (the `wlanNmon` airmon-ng created). Returns it, or None if that phy has
        no monitor vif (caller falls back to base_iface). Two passes: find
        base_iface's phy, then the monitor interface on the same phy."""
        target_phy = None
        current_phy = None
        for line in iw_text.splitlines():
            if line.startswith("phy"):
                current_phy = line.strip().split('#')[-1]
            if f"Interface {base_iface}" in line:
                target_phy = current_phy
                break
        current_phy = None
        current_iface = None
        for line in iw_text.splitlines():
            if line.startswith("phy"):
                current_phy = line.strip().split('#')[-1]
            if "Interface " in line:
                current_iface = line.split("Interface ")[1].strip()
            if "type monitor" in line and current_phy == target_phy:
                return current_iface
        return None

    @staticmethod
    def detect_5g(iwlist_text):
        """True if `iwlist <iface> freq` lists a 5 GHz channel. 5 GHz frequencies
        render as `5.NNN GHz`; no 2.4 GHz channel frequency contains `5.`."""
        return "5." in iwlist_text

    @staticmethod
    def next_capture_paths(dest_dir):
        """Next free (capture-N.pcap, capture-N_logs) pair in dest_dir — N steps
        past existing pcaps so a repeat run never clobbers a prior capture."""
        count = 1
        while (dest_dir / f"capture-{count}.pcap").exists():
            count += 1
        return (dest_dir / f"capture-{count}.pcap",
                dest_dir / f"capture-{count}_logs")

    @staticmethod
    def parse_modinfo(text):
        """modinfo `key: value` lines → dict (first value wins per key)."""
        out = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                if key and key not in out:
                    out[key] = val.strip()
        return out

    @staticmethod
    def parse_modinfo_firmware(text):
        """All `firmware:` blob names the module declares (a driver can request
        several — e.g. a main FW + a calibration table)."""
        names = []
        for line in text.splitlines():
            if line.startswith("firmware:"):
                name = line.partition(":")[2].strip()
                if name:
                    names.append(name)
        return names

    @staticmethod
    def lsusb_diff(before, after):
        """`lsusb` lines in `after` but not `before` — the device(s) that
        appeared since the baseline. Chipset-agnostic: whatever shows up after
        the card is plugged IS the card, no VID:PID hardcoding."""
        before_lines = set(before.splitlines())
        return [line for line in after.splitlines()
                if line.strip() and line not in before_lines]

    def throw(self, msg):
        self.logger.log_main(f"\n[ERROR] {msg}")
        self.cleanup()
        sys.exit(1)

    @staticmethod
    def _lsusb():
        try:
            return subprocess.run(["lsusb"], capture_output=True, text=True).stdout
        except FileNotFoundError:
            return ""

    def snapshot_usb(self, tag):
        """Append the full `lsusb` listing to usb-topology.log under `tag`, and
        log any device that appeared since the pre-plug baseline to main.log.
        Run at a few points so a post-FW re-enumeration (the card jumping
        bus/address) is visible."""
        current = self._lsusb()
        with open(self.temp_dir / "usb-topology.log", "a") as f:
            f.write(f"===== lsusb [{tag}] =====\n{current}\n")
        for line in self.lsusb_diff(self.lsusb_baseline, current):
            self.logger.log_main(f"[USB +{tag}] {line.strip()}")

    @staticmethod
    def _bound_module(iface):
        """Kernel module bound to `iface` (via /sys/class/net/<iface>/device/
        driver) — chipset-agnostic, no hardcoded module list."""
        if not iface:
            return None
        link = Path("/sys/class/net") / iface / "device" / "driver"
        try:
            return os.path.basename(os.readlink(link))
        except OSError:
            return None

    def log_driver_info(self, iface):
        """Record the module bound to `iface` (modinfo: filename/version/
        srcversion/vermagic + the firmware blobs it requests) and a dmesg tail,
        so we know exactly what produced the pcap. The version fields are the
        fetch recipe for mainline drivers that ship no on-disk source."""
        self.driver_module = self._bound_module(iface)
        drv_log = self.temp_dir / "driver.log"
        try:
            with open(drv_log, "w") as f:
                if self.driver_module:
                    info = subprocess.run(["modinfo", self.driver_module],
                                          capture_output=True, text=True)
                    if info.returncode == 0:
                        f.write(f"===== modinfo {self.driver_module} =====\n{info.stdout}\n")
                        fields = self.parse_modinfo(info.stdout)
                        self.firmware_files = self.parse_modinfo_firmware(info.stdout)
                        for k in ("filename", "version", "srcversion", "vermagic"):
                            if fields.get(k):
                                self.logger.log_main(f"[DRIVER {self.driver_module}] {k}: {fields[k]}")
                        if self.firmware_files:
                            self.logger.log_main(f"[DRIVER {self.driver_module}] firmware: {', '.join(self.firmware_files)}")
                else:
                    self.logger.log_main("[DRIVER] could not determine the bound module")
                dm = subprocess.run(["dmesg"], capture_output=True, text=True)
                f.write("\n===== dmesg (last 120 lines) =====\n")
                f.write("\n".join(dm.stdout.splitlines()[-120:]))
        except (FileNotFoundError, OSError) as e:
            self.logger.log_main(f"[DRIVER] info capture skipped: {e}")

    def _dkms_source_dir(self):
        """On-disk source tree for the bound module if it's an out-of-tree /
        DKMS driver (mainline drivers ship no buildable source on the box).
        Resolves via `dkms status` first, then a /usr/src glob."""
        if not self.driver_module:
            return None
        try:
            out = subprocess.run(["dkms", "status"], capture_output=True, text=True).stdout
        except FileNotFoundError:
            out = ""
        for line in out.splitlines():
            name_ver = line.split(",")[0].strip()
            if "/" in name_ver:
                name, ver = name_ver.split("/", 1)
                cand = Path("/usr/src") / f"{name.strip()}-{ver.strip()}"
                if cand.is_dir():
                    return cand
        # Fallback: a /usr/src dir whose name echoes the module (e.g. 8814au-*).
        stem = self.driver_module.split("_")[-1]
        for cand in sorted(Path("/usr/src").glob(f"*{stem}*")):
            if cand.is_dir():
                return cand
        return None

    def collect_driver_artifacts(self, dest_dir):
        """Copy the bound driver's firmware blobs (and DKMS source, if on disk)
        into dest_dir so each capture carries the exact artifacts that produced
        it. Mainline drivers ship no on-disk source — driver.log's vermagic +
        filename is the fetch recipe instead."""
        if self.firmware_files:
            fw_dir = dest_dir / "firmware"
            for name in self.firmware_files:
                src = Path("/lib/firmware") / name
                dst = fw_dir / Path(name).name
                if src.exists() and not dst.exists():
                    fw_dir.mkdir(exist_ok=True)
                    try:
                        shutil.copy(src, dst)
                        self.logger.log_main(f"[+] Firmware: {src} -> {dst}")
                    except OSError as e:
                        self.logger.log_main(f"[!] Firmware copy failed ({src}): {e}")
                elif not src.exists():
                    self.logger.log_main(f"[!] Firmware declared but not on disk: {src}")

        src_tree = self._dkms_source_dir()
        dst_tree = dest_dir / "driver-source"
        if src_tree and not dst_tree.exists():
            try:
                shutil.copytree(src_tree, dst_tree)
                self.logger.log_main(f"[+] Driver source: {src_tree} -> {dst_tree}")
            except OSError as e:
                self.logger.log_main(f"[!] Source copy failed ({src_tree}): {e}")
        elif not src_tree:
            self.logger.log_main("[*] No on-disk driver source (mainline?) — use "
                                 "driver.log vermagic/filename to fetch matching source.")

    def run_cmd(self, cmd_list, fatal=False, timeout=60):
        """Run a command, logging exactly when it started/finished (main.log
        `Running: <cmd>` + the per-tool log), then pause 1 s before returning.

        No fixed schedule: main.log's per-event epochs are the source of truth
        for slicing the pcap afterwards, so commands just run back-to-back. On
        timeout we log and carry on (the capture so far is never discarded)
        unless fatal=True — reserved for the one must-succeed step, monitor-mode
        bring-up."""
        self.logger.log_main(f"Running: {' '.join(cmd_list)}")
        start_exec = time.time()
        timed_out = False
        try:
            res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)
            combined_output = (res.stdout or "") + (res.stderr or "")
            return_code = res.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            combined_output = f"[TIMEOUT after {timeout}s]"
            return_code = -1

        self.logger.log_cmd(cmd_list, combined_output, return_code, start_exec, time.time() - start_exec)

        if timed_out:
            self.logger.log_main(
                f"[!] TIMEOUT: '{' '.join(cmd_list)}' exceeded {timeout}s"
                + ("" if fatal else " (continuing)"))
            if fatal:
                self.throw(f"Fatal command timed out: {' '.join(cmd_list)}")

        time.sleep(1)
        return combined_output

    def airodump_segment(self, duration=20.0):
        """Let airodump-ng hop on its own (its native DEFAULT_HOPFREQ = 250 ms,
        via wi_set_channel -> nl80211 -> the kernel's set_channel) for `duration`
        seconds, so the capture contains the KERNEL's per-hop register burst at
        the same 250 ms cadence wifit3 uses. `--band abg` = all bands (a=5 GHz,
        b/g=2.4 GHz). No -w: we only care about the channel-set USB traffic."""
        self.logger.log_main(f"[{time.time():.3f}] [AIRODUMP] start --band abg ({duration}s @ 250ms native hop)")
        self.airodump_log = open(self.temp_dir / "airodump-ng.log", "w")
        self.airodump_proc = subprocess.Popen(
            ["sudo", "airodump-ng", "--band", "abg", self.mon_iface],
            stdout=self.airodump_log, stderr=self.airodump_log,
        )
        time.sleep(duration)
        # Stop airodump before the deterministic iw hopping takes over the iface.
        subprocess.run(["sudo", "pkill", "-P", str(self.airodump_proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "kill", str(self.airodump_proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.airodump_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        self.airodump_proc = None
        self.logger.log_main(f"[{time.time():.3f}] [AIRODUMP] stopped")

    def fast_hop_segment(self, dwell=0.25, duration=12.0, channels=(1, 6, 11)):
        """Hop at the wifit3 TUI's pathological 0.25 s cadence to test whether
        the KERNEL also goes silent (relock > dwell) or keeps capturing. Uses its
        own sleep loop so it can never abort the capture; each hop is timestamped
        into main.log so pcap_slicer can isolate the window.
        """
        self.logger.log_main(f"[FAST-HOP] {len(channels)} chans @ {dwell}s for {duration}s")
        end = time.time() + duration
        i = 0
        while time.time() < end:
            ch = channels[i % len(channels)]
            i += 1
            t0 = time.time()
            self.logger.log_main(f"[{t0:.3f}] FAST-HOP set channel {ch}")
            subprocess.run(["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            remain = dwell - (time.time() - t0)
            if remain > 0:
                time.sleep(remain)

    def _save_artifacts(self, dest_dir):
        """Move the pcap and copy the per-tool logs into dest_dir (next free
        capture-N slot)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_pcap, final_logs = self.next_capture_paths(dest_dir)
        tmp_pcap = self.temp_dir / "capture.pcap"
        if tmp_pcap.exists():
            subprocess.run(["sudo", "mv", str(tmp_pcap), str(final_pcap)])
            self.logger.log_main(f"[+] Saved capture to: {final_pcap}")
        final_logs.mkdir(exist_ok=True)
        for log_file in self.temp_dir.glob("*.log"):
            shutil.copy(log_file, final_logs)
        return dest_dir

    def cleanup(self):
        self.logger.log_main("\n[*] Teardown initiated. Cleaning up...")
        # Kill airodump first if a throw landed mid-segment (it owns the iface).
        if self.airodump_proc and self.airodump_proc.poll() is None:
            subprocess.run(["sudo", "pkill", "-P", str(self.airodump_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "kill", str(self.airodump_proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.airodump_log:
            self.airodump_log.close()
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

        self.logger.log_main("[+] Cleanup complete. Safe to unplug.")

        tmp_pcap = self.temp_dir / "capture.pcap"
        if self.chipset == "unknown":
            # airmon-ng never bound the card → no useful capture. Don't clutter
            # the repo with a captures_unknown/ dir; salvage to /tmp if there's
            # anything, so it's recoverable but out of the way.
            if tmp_pcap.exists():
                salvage = Path(f"/tmp/wifit3_unsaved_{int(time.time())}")
                self.logger.log_main(f"[!] No chipset detected (airmon never bound the "
                                     f"card) — NOT saving to the repo. Artifacts at: {salvage}")
                self._save_artifacts(salvage)
            else:
                self.logger.log_main("[*] No chipset and no pcap — nothing to save.")
        else:
            dest_dir = Path(__file__).parent / f"captures_{self.chipset}"
            self._save_artifacts(dest_dir)
            self.collect_driver_artifacts(dest_dir)
            # Hand the whole dir back to the invoking user (we ran under sudo).
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
        self.logger.log_main("\n[*] Capture started.")

        # tshark on usbmon0 (all buses — survives a post-FW re-enumeration onto a
        # different bus). The pcap is written to the RAM-backed temp dir and only
        # moved to the capture dir at teardown, after tshark stops.
        pcap_path = self.temp_dir / "capture.pcap"
        self.tshark_log = open(self.temp_dir / "tshark.log", "w")
        self.tshark_proc = subprocess.Popen(
            ["sudo", "tshark", "-i", self.USBMON, "-w", str(pcap_path), "-q"],
            stdout=self.tshark_log, stderr=self.tshark_log
        )

        # Baseline the USB tree (our card not plugged yet) so snapshot_usb can
        # report exactly what appears — chipset-agnostic device identification.
        self.lsusb_baseline = self._lsusb()

        # Give the operator time to plug the card in and let it enumerate before
        # bringing up monitor mode.
        self.logger.log_main(f"[{time.time():.3f}] --> INSERT THE USB CARD NOW <--")
        time.sleep(self.PLUG_IN_WAIT)

        # What appeared on the bus is the card.
        self.snapshot_usb("post-plug")

        # Monitor-mode bring-up — the one step that must succeed.
        self.run_cmd(["sudo", "airmon-ng", "start", self.BASE_IFACE], fatal=True, timeout=30)

        # If the bus/device changed here, airmon re-enumerated the card (the
        # usbmon0 all-buses capture still catches it).
        self.snapshot_usb("post-airmon")

        # --- Interface / band / chipset detection ---
        iw_out = subprocess.run(["iw", "dev"], capture_output=True, text=True).stdout
        self.mon_iface = self.parse_monitor_iface(iw_out, self.BASE_IFACE)
        if not self.mon_iface:
            self.logger.log_main(f"[!] Warning: no monitor interface for {self.BASE_IFACE}. Falling back to {self.BASE_IFACE}.")
            self.mon_iface = self.BASE_IFACE

        freq_out = subprocess.run(["iwlist", self.mon_iface, "freq"], capture_output=True, text=True).stdout
        self.supports_5g = self.detect_5g(freq_out)
        self.logger.log_main("[*] 5GHz Support Detected." if self.supports_5g
                             else "[!] 5GHz Support NOT detected. Skipping 5GHz sequence.")

        airmon_check = subprocess.run(["airmon-ng"], capture_output=True, text=True).stdout
        self.chipset = self.parse_chipset(airmon_check, self.BASE_IFACE) or "unknown"
        self.logger.log_main(f"[*] Detected Monitor Interface: {self.mon_iface}")
        self.logger.log_main(f"[*] Detected Chipset/Driver:  {self.chipset}")

        # Record the bound driver (modinfo version fields + firmware) and dmesg,
        # now that the interface name is known.
        self.log_driver_info(self.mon_iface)

        # Opt-in: the kernel's native 250 ms airodump hop, a reference for diffing
        # our set_channel cadence.
        if self.debug_segments:
            self.airodump_segment()

        # 2.4 GHz hops, one per channel.
        for ch in range(1, 13):
            self.run_cmd(["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)], timeout=10)

        # 5 GHz hops (if supported).
        if self.supports_5g:
            channels_5g = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                           116, 120, 124, 128, 132, 136, 140, 144, 149, 153,
                           157, 161, 165]
            for ch in channels_5g:
                self.run_cmd(["sudo", "iw", "dev", self.mon_iface, "set", "channel", str(ch)], timeout=10)

        # Back to channel 1 for the injection tests.
        self.run_cmd(["sudo", "iw", "dev", self.mon_iface, "set", "channel", "1"], timeout=10)

        # Injection test + a single deauth — only with a --target, and both
        # non-fatal so a slow/failing aireplay never discards the capture.
        if self.target_bssid:
            self.run_cmd(["sudo", "aireplay-ng", "-a", self.target_bssid, "--test", self.mon_iface], timeout=60)
            if self.client_bssid:
                self.run_cmd(["sudo", "aireplay-ng", "-0", "1",
                              "-a", self.target_bssid, "-c", self.client_bssid, self.mon_iface], timeout=30)
            else:
                self.logger.log_main("[*] No --client given; skipping deauth.")
        else:
            self.logger.log_main("[*] No --target given; skipping injection test + deauth.")

        # Opt-in: 0.25 s fast-hop stress (does the kernel survive the TUI's hop
        # cadence?). Last, so it can't disturb the clean per-hop reference above.
        if self.debug_segments:
            self.fast_hop_segment()

        self.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="Wifit3 automated USB capture tool (run as root).")
    parser.add_argument("--target", help="AP BSSID for the aireplay injection test + deauth")
    parser.add_argument("--client", help="client BSSID for the deauth (needs --target too)")
    parser.add_argument("--debug-segments", action="store_true",
                        help="also run the airodump + fast-hop investigation segments (~20 MB)")
    args = parser.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("This tool needs root (usbmon / airmon-ng / tshark). Run with:\n"
              "    sudo python capture.py [--target BSSID] [--client BSSID]")
        sys.exit(1)

    app = None
    try:
        app = Capture(target=args.target, client=args.client,
                      debug_segments=args.debug_segments)
        app.run()
    except KeyboardInterrupt:
        # Use stdout.write to ensure clean newline even in raw mode
        sys.stdout.write("\r\n[!] Ctrl+C caught. Exiting.\r\n")
        if app is not None:
            app.cleanup()
        sys.exit(1)


if __name__ == "__main__":
    main()
