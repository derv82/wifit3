"""Cold-boot usbmon capture for the MT7601U, keyed on USB VID:PID instead of netdev.

capture.py identifies the card by diffing `iw dev` names, which silently falls
back to the wrong adapter when the name is already present. Here the card is
found by its USB id and its netdev is looked up from sysfs, so the monitor-mode
bring-up and the channel sweep always land on the dongle.

    sudo .venv/bin/python scratch/capture_mt7601u.py
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
USB_ID = "148f:7601"
USBMON = "usbmon0"
PLUG_WAIT_S = 0            # 0 = wait indefinitely; a timeout just loses the capture
SETTLE_S = 12.0
AIRODUMP_S = 10


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def device_present() -> bool:
    return USB_ID in run(["lsusb"]).stdout.lower()


def dongle_netdev() -> str | None:
    """The netdev the dongle owns, matched by USB VID:PID through sysfs.

    `/sys/class/net/<name>/device` points at the USB *interface* node (1-4:1.0), and
    the `net/` attribute lives on that node, not on the USB device node (1-4) that
    carries idVendor/idProduct -- so match on the netdev side and walk up to the
    device node instead of the other way round.
    """
    net_root = Path("/sys/class/net")
    if not net_root.is_dir():
        return None
    for entry in net_root.iterdir():
        dev = entry / "device"
        if not dev.exists():
            continue
        node = dev.resolve()
        for parent in [node, *node.parents]:
            vid, pid = parent / "idVendor", parent / "idProduct"
            if not (vid.is_file() and pid.is_file()):
                continue
            try:
                pair = f"{vid.read_text().strip()}:{pid.read_text().strip()}"
            except OSError:
                continue
            if pair.lower() == USB_ID:
                return entry.name
            break                      # found the nearest USB ancestor; wrong id
    return None


def wait_for_netdev(timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        name = dongle_netdev()
        if name:
            return name
        time.sleep(0.5)
    return None


def monitor_name(base: str) -> str:
    """airmon-ng names the monitor vif <base>mon; fall back to the phy scan."""
    out = run(["sudo", "iw", "dev"]).stdout
    parsed = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            parsed.append(line.split()[1])
    for candidate in parsed:
        if candidate.startswith(base):
            return candidate
    return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="driver_captures_mt7601u/capture-2")
    ap.add_argument("--skip-injection", action="store_true", default=True)
    ap.add_argument("--wait", type=int, default=PLUG_WAIT_S,
                    help="seconds to wait for the dongle; 0 waits forever")
    ap.add_argument("--attach", action="store_true",
                    help="card is already plugged in and bound: capture monitor "
                         "mode + the channel sweep only, skipping the cold boot")
    args = ap.parse_args()

    if USBMON not in run(["sudo", "tshark", "-D"]).stdout:
        print(f"[!] {USBMON} not offered by tshark; is debugfs mounted and usbmon loaded?",
              file=sys.stderr)
        return 2

    if device_present() and not args.attach:
        print(f"[!] {USB_ID} is ALREADY plugged in. Unplug it, then re-run")
        print("    without --attach, or pass --attach to capture only the")
        print("    monitor-mode entry and the channel sweep.")
        return 2

    outdir = REPO / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    pcap = outdir / "capture.pcap"

    print("--- MT7601U cold-boot capture ---")
    print(f"[!] Baseline: {USB_ID} absent. Good.")
    print("[*] Starting usbmon0 capture.")
    tshark = subprocess.Popen(
        ["sudo", "tshark", "-i", USBMON, "-w", str(pcap), "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)

    t_start = time.time()
    if args.attach:
        print(f"\n[*] --attach: using the already-present {USB_ID}.")
        t_enumerate = t_start
    else:
        window = f"{args.wait}s" if args.wait else "indefinitely (Ctrl-C to abort)"
        print(f"\n[*] PLUG THE DONGLE IN NOW -- waiting {window} ...", flush=True)
        deadline = time.time() + args.wait
        while not device_present():
            if args.wait and time.time() > deadline:
                break
            time.sleep(0.5)
        if not device_present():
            print("[!] Timed out waiting for the dongle. Aborting.")
            tshark.terminate()
            return 1
        t_enumerate = time.time()
        print(f"[+] {USB_ID} appeared {t_enumerate - t_start:.1f}s after the prompt.")

    base = wait_for_netdev(30.0)
    if not base:
        print("[!] The dongle is present but no netdev is bound to it.")
        print("    Check: airmon-ng   (expect a phy row with driver mt7601u)")
        tshark.terminate()
        return 1
    print(f"[+] Dongle netdev: {base}")
    if not args.attach:
        print(f"[*] Letting the kernel driver finish bring-up ({SETTLE_S:.0f}s) ...")
        time.sleep(SETTLE_S)

    print(f"[*] airmon-ng start {base}")
    proc = run(["sudo", "airmon-ng", "start", base], timeout=60)
    print(proc.stdout.strip() or proc.stderr.strip())
    time.sleep(3.0)

    mon = monitor_name(base)
    print(f"[*] Monitor interface: {mon}")

    log = outdir / "main.log"
    with log.open("w") as fh:
        fh.write(f"[{t_start:.3f}] capture start\n")
        fh.write(f"[{t_enumerate:.3f}] {USB_ID} enumerated\n")
        fh.write(f"[{time.time():.3f}] monitor vif {mon}\n")

    print(f"[*] airodump-ng hopping {AIRODUMP_S}s ...")
    run(["sudo", "timeout", str(AIRODUMP_S), "airodump-ng",
         "--band", "abg", mon], timeout=AIRODUMP_S + 20)

    print("[*] Per-channel sweep (the byte-match target for set_channel).")
    sweep = []
    for ch in range(1, 15):                       # MT7601U is 2.4 GHz only
        t0 = time.time()
        proc = run(["sudo", "iw", "dev", mon, "set", "channel", str(ch)], timeout=30)
        sweep.append((ch, time.time() - t0, proc.returncode, proc.stderr.strip()))
        with log.open("a") as fh:
            fh.write(f"[{t0:.3f}] set channel {ch} rc={proc.returncode} {proc.stderr.strip()}\n")
        print(f"    ch{ch:<3d} rc={proc.returncode} {proc.stderr.strip()}")
        time.sleep(0.4)

    print("[*] Stopping tshark ...")
    tshark.terminate()
    tshark.wait(timeout=30)

    print("[*] Teardown.")
    run(["sudo", "airmon-ng", "stop", mon], timeout=60)

    size = pcap.stat().st_size if pcap.exists() else 0
    print(f"\n[+] Saved {pcap} ({size} bytes)")
    ok = [c for c, _t, rc, _e in sweep if rc == 0]
    print(f"[+] {len(ok)}/{len(sweep)} channel tunes returned rc=0")
    if len(ok) != len(sweep):
        print("[!] Some tunes failed; check main.log before trusting this bundle.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())