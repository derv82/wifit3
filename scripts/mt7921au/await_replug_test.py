"""
Autonomous watcher: wait for the next physical replug (unplug -> replug), then run
test_hw once on the cold device — which now carries the epctl_rst_opt + MT_SWDEF_MODE
fixes. One-shot, so it doesn't keep grabbing the device. Result -> replug_test_result.txt.
"""
import sys
import time
import subprocess

import libusb_package
import usb.core

VID, PID = 0x0E8D, 0x7961
backend = libusb_package.get_libusb1_backend()
TIMEOUT = 3600


def present():
    try:
        return usb.core.find(idVendor=VID, idProduct=PID, backend=backend) is not None
    except Exception:
        return False


def wait(cond, label):
    start = time.time()
    while time.time() - start < TIMEOUT:
        if cond():
            time.sleep(1)
            if cond():   # debounce
                return True
        time.sleep(2)
    print(f"await_replug: timed out waiting for {label}", flush=True)
    return False


print("await_replug: watching for unplug...", flush=True)
if not present():
    print("await_replug: device already absent — waiting for (re)plug directly", flush=True)
elif not wait(lambda: not present(), "unplug"):
    sys.exit(0)
print("await_replug: unplugged. waiting for replug...", flush=True)
if not wait(present, "replug"):
    sys.exit(0)
time.sleep(4)  # let it settle/enumerate
print("await_replug: REPLUG detected — running test_hw (epctl + SWDEF_MODE fixes)...", flush=True)

r = subprocess.run([sys.executable, "scripts/mt7921au/test_hw_mt7921au.py"],
                   capture_output=True, text=True)
out = r.stdout + r.stderr
with open("scripts/mt7921au/replug_test_result.txt", "w") as f:
    f.write(out)

KEYS = ("speed", "epctl", "FW_START", "N9_RDY", "FW_N9", "[PASS]", "[FAIL]",
        "Short bulk", "ALL STEPS", "no response", "powered", "Received")
print("=== test_hw result (key lines) ===", flush=True)
for line in out.splitlines():
    if any(k in line for k in KEYS):
        print("  " + line, flush=True)
verdict = "FW BOOTED — FW_N9_RDY reached!!!" if "ALL STEPS PASSED" in out or "Received" in out \
    else ("connect() failed (no boot)" if "[FAIL]" in out else "see file")
print(f"await_replug: VERDICT — {verdict}", flush=True)
