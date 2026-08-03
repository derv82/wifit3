"""capture_our_hops.py -- start tshark USB capture, run wifit3 doing channel
hops, stop tshark. Output: a .pcap of OUR driver's wire traffic, capturable
side-by-side with the kernel's `driver_captures/captures_mt76x0u/capture-2.pcap`.

The hop sequence matches what the kernel's main.log shows for capture-2:
ch 1, 2, 3, 4, 5, 6 with 1-second gaps between iw set channel commands.
That makes direct frame-by-frame slicing possible.

Usage:
    uv run python scripts/chips/mt76x0u/capture_our_hops.py --usbpcap USBPcap2

Output files (timestamped, next to the script's own captures dir):
    ours-YYYYMMDD-HHMMSS.pcap        raw USB capture
    ours-YYYYMMDD-HHMMSS.main.log    log mimicking the kernel's main.log
                                     format, so pcap_slicer.py works on
                                     ours just like on the kernel pcap.

After running, slice and compare:
    uv run python scripts/porting/pcap_slicer.py \
        scripts/chips/mt76x0u/captures_ours/ours-*.main.log \
        scripts/chips/mt76x0u/captures_ours/ours-*.pcap
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

# Set wire-log path BEFORE importing the driver. wire_log.py reads this env
# var at module import time. Shared timestamp keeps the .pcap, .main.log
# and .wire.txt filenames in sync.
_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
_OUT_DIR = Path(__file__).parent / "captures_ours"
_OUT_DIR.mkdir(exist_ok=True)
_WIRE_LOG_PATH = _OUT_DIR / f"ours-{_STAMP}.wire.txt"
os.environ["WIFIT3_WIRE_LOG_FILE"] = str(_WIRE_LOG_PATH)

import libusb_package
import usb.core

from wifit3.chips.mt76x0u.constants import USB_IDS_MT76X0U
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.driver import DeviceID


def find_device():
    backend = libusb_package.get_libusb1_backend()
    for vid, pid, chipset, vendor, product in USB_IDS_MT76X0U:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            return found, DeviceID(vid, pid, chipset, vendor, product)
    return None, None


def start_tshark(iface: str, pcap_path: Path) -> subprocess.Popen:
    """Start tshark capturing on `iface` to `pcap_path`. Returns the
    Popen handle so we can terminate it later. Includes a brief delay
    to let tshark actually start listening before the driver does USB
    traffic.
    """
    print(f"[*] starting tshark on {iface} -> {pcap_path}")
    cmd = [
        "tshark",
        "-i", iface,
        "-w", str(pcap_path),
        # No display filter -- capture everything on this USB bus; we slice
        # later by device address.
        "-q",       # quiet, less stderr noise
        "-F", "pcap",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # Need this on Windows so we can cleanly Ctrl+C / terminate it
        # without killing this script's process group.
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    # Brief settle -- tshark needs ~500 ms to initialize the capture
    # before it starts writing frames.
    time.sleep(1.5)
    if proc.poll() is not None:
        # tshark exited early -- print whatever it said
        err = proc.stderr.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"tshark exited early (returncode={proc.returncode}). "
                           f"stderr:\n{err}")
    print(f"[*] tshark pid={proc.pid} running")
    return proc


def stop_tshark(proc: subprocess.Popen) -> None:
    print(f"[*] stopping tshark pid={proc.pid}")
    if hasattr(signal, "CTRL_BREAK_EVENT"):
        # Windows: clean SIGBREAK; tshark closes the pcap properly
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("[*] tshark stopped")


def write_main_log(log_path: Path, events: list[tuple[float, str]]) -> None:
    """Mimic kernel main.log format so pcap_slicer.py works on our pcap.
    Format (per scripts/porting/pcap_slicer.py:8):
        [epoch] [T=X.XXs] Running: <cmd>
    Plus a hardware-plugin marker at T=0.
    """
    with open(log_path, "w") as f:
        if not events:
            return
        t0 = events[0][0]
        f.write(f"[{t0:.3f}] --> INSERT THE USB CARD NOW <--\n")
        for epoch, cmd in events:
            rel = epoch - t0
            f.write(f"[{epoch:.3f}] [T={rel:.2f}s] Running: {cmd}\n")


async def main_async(args):
    # Use the timestamp from module init so .pcap, .main.log, .wire.txt
    # share a stem — the wire log path was already plumbed via env var.
    out_dir = _OUT_DIR
    stamp = _STAMP
    pcap_path = out_dir / f"ours-{stamp}.pcap"
    log_path = out_dir / f"ours-{stamp}.main.log"
    wire_log_path = _WIRE_LOG_PATH

    dev, id_entry = find_device()
    if dev is None:
        print("[FATAL] no MT76x0U device found")
        return 2
    print(f"[*] found {id_entry.description} ({id_entry.vid:04x}:{id_entry.pid:04x})")

    # Start tshark FIRST -- we want the FW upload + init + hops all captured.
    tshark_proc = start_tshark(args.usbpcap, pcap_path)

    # main.log-style event timeline so the slicer can map timestamps back
    # to frame ranges.
    events: list[tuple[float, str]] = []

    try:
        driver = MT76x0UDriver.from_usb_device(dev, id_entry)

        events.append((time.time(), "driver.connect() [cold or warm-reattach]"))
        print("[*] driver.connect()")
        ok = await driver.connect()
        if not ok:
            print("[FATAL] connect() returned False")
            return 3

        # Stabilize -- give the chip a moment to settle, capture some baseline
        # RX, before the hop sequence starts.
        time.sleep(1.0)

        for ch in args.channels:
            cmd = f"driver.set_channel({ch})"
            events.append((time.time(), cmd))
            print(f"[*] {cmd}")
            t0 = time.monotonic()
            ok = await driver.set_channel(ch)
            elapsed = (time.monotonic() - t0) * 1000
            print(f"    -> ok={ok}, set_channel took {elapsed:.0f}ms")
            # 1-second gap between hops, matching kernel main.log cadence.
            time.sleep(1.0)

        # Final settle so any post-hop traffic gets captured.
        time.sleep(1.0)

        await driver.close()

    finally:
        # Brief wait so tshark flushes the last frames.
        time.sleep(0.5)
        stop_tshark(tshark_proc)
        write_main_log(log_path, events)

    print()
    print(f"[+] capture saved:  {pcap_path}")
    print(f"[+] event log:      {log_path}")
    print(f"[+] wire log:       {wire_log_path}")
    print()
    print("Next steps:")
    print(f"  uv run python scripts/porting/pcap_slicer.py {log_path} {pcap_path}")
    print(f"  # then tshark -r {pcap_path} -Y 'frame.number >= N and frame.number <= M'")
    print()
    print("Compare against kernel pcap:")
    print("  uv run python scripts/porting/pcap_slicer.py \\")
    print("    driver_captures/captures_mt76x0u/capture-2_logs/main.log \\")
    print("    driver_captures/captures_mt76x0u/capture-2.pcap")

    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usbpcap", default="USBPcap2",
                        help="Which USBPcap bus to capture on. Try USBPcap1 if "
                             "USBPcap2 doesn't see your dongle. (default: USBPcap2)")
    parser.add_argument("--channels", type=int, nargs="+",
                        default=[1, 2, 3, 4, 5, 6],
                        help="Channel sequence to hop through. Default: 1..6, "
                             "matching the kernel's capture-2 first 6 hops "
                             "for easy frame-by-frame comparison.")
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
