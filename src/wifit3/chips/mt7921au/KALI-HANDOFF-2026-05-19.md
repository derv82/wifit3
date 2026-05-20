# MT7921AU Bring-up — Kali Test Run Handoff

**Date:** 2026-05-19
**Host:** Kali 2026.1 live USB (kernel `6.18.12+kali-amd64`)
**Device:** MediaTek MT7921AU, VID:PID `0e8d:7961`, Bus 4 Device 26
**Repo state:** Same code as the 2026-05-17 session-pause snapshot. No further changes made before this test.

## TL;DR

**Result:** Test failed on Kali too — earlier than on Windows. We never even completed the first MCU command (`PATCH_SEM_CONTROL`/`PATCH_SEM_GET`, cid `0x10`, seq `0x01`); the bulk OUT to EP `0x08` returned an error and we got no response on EP `0x85`.

This **does not match** what the snapshot hypothesized would happen on Kali. The "WinUSB-specific post-FW_START_REQ event" hypothesis (#1) and "shallow bulk-IN URB pool" hypothesis (#2) are **not sufficient** to explain this — we're failing well before FW_START_REQ, and we're failing on the host where the kernel driver demonstrably works fine on this exact device.

Most likely culprit: **the kernel `mt7921u` driver was still bound to the device when the test ran.** Evidence below. Two follow-ups for the Windows-land session: confirm/clear that on the next Kali run, and if it persists, this becomes a genuine isolated code bug — which the snapshot called out as the alternative branch ("genuine code bug we haven't isolated yet").

## What was tested

```bash
sudo .venv/bin/python scratch/test_hw_mt7921au.py
```

(Running from the project root on Kali, against the device freshly enumerated on Bus 4 at SuperSpeed.)

## USB topology at test time

```
lsusb -d 0e8d:7961
Bus 004 Device 026: ID 0e8d:7961 MediaTek Inc. Wireless_Device
```

```
lsusb -t  (relevant subtree)
/:  Bus 004.Port 001: Dev 001, Class=root_hub, Driver=xhci_hcd/10p, 20000M/x2
    |__ Port 002: Dev 026, If 0, Class=Wireless, Driver=btusb, 5000M
    |__ Port 002: Dev 026, If 1, Class=Wireless, Driver=btusb, 5000M
    |__ Port 002: Dev 026, If 2, Class=Wireless, Driver=btusb, 5000M
    |__ Port 002: Dev 026, If 3, Class=Vendor Specific Class, Driver=[none], 5000M
```

Confirmed:
- Device negotiated **SuperSpeed (5 Gbps)** — same as the snapshot's lsusb_verbose for the working Linux load.
- Interface 3 shows `Driver=[none]` here, which **looks** like the kernel driver is detached. Interfaces 0/1/2 are claimed by `btusb` (Bluetooth functions of the combo chip — expected).

**Caveat:** `Driver=[none]` on If 3 in `lsusb -t` is necessary but not sufficient to prove `mt7921u` is fully out of the way. See "Probable root cause" below — the dmesg story contradicts the lsusb snapshot.

## Test output

```
sudo .venv/bin/python scratch/test_hw_mt7921au.py
--- USB Discovery ---
[PASS] Found MT7921AU at bus 4, address 26
--- connect() [60s timeout] ---
  [ 10.0%] Uploading Firmware...
18:51:19.647 [INFO ] wifit3.chips.mt7921au.driver: Initializing MT7921AU...
18:51:19.647 [INFO ] wifit3.chips.mt7921au.firmware: Starting MT7921AU firmware upload sequence...
18:51:19.647 [INFO ] wifit3.chips.mt7921au.firmware: Claiming interface 3...
18:51:19.924 [INFO ] wifit3.chips.mt7921au.firmware: MT7921 detected: chip_id=0x7961
18:51:19.925 [INFO ] wifit3.chips.mt7921au.firmware: Sending MCU power-on...
18:51:19.937 [INFO ] wifit3.chips.mt7921au.firmware: MCU powered on.
18:51:19.947 [INFO ] wifit3.chips.mt7921au.firmware: Patch: build_date='20250625153620a\n' n_region=1
18:51:19.986 [ERROR] wifit3.chips.mt7921au.transport: MCU send_bulk failed (cid=0x10 seq=0x01)
18:51:19.986 [ERROR] wifit3.chips.mt7921au.firmware: PATCH_SEM_CONTROL get: no response
18:51:19.986 [ERROR] wifit3.chips.mt7921au.driver: Failed to load MT7921AU firmware.
[FAIL] connect() returned False — check logs above for the failing step
```

Failure point: **first MCU command of the entire upload sequence** — the `PATCH_SEM_GET` (cid `0x10`, seq `0x01`) on EP `0x08`. This is the same byte-for-byte command that diffed clean against capture-3 frame 14182 per the 2026-05-17 snapshot.

What did succeed before the failure:
- Interface 3 claim
- Vendor read returning chip_id `0x7961`
- "MCU power-on" (whatever the current code does for this — likely a vendor-request register write, not bulk traffic)
- Parsing the patch blob header (`build_date='20250625153620a\n' n_region=1` — `n_region=1` matches the `_1_` patch variant, confirming the refreshed firmware blob loaded correctly from `assets/`)

What failed:
- `transport.MCU send_bulk failed (cid=0x10 seq=0x01)` — the actual `dev.write()` to EP `0x08` errored out (the log says "send_bulk failed", not "send succeeded but no response", so it failed at the OUT, not the IN).

## dmesg timeline

This is where it gets interesting. The dmesg log shows two complete bring-up cycles for this device, neither of which lines up with the test run cleanly.

```
[ 5277.259605] Bluetooth: hci1: Device setup in 123186 usecs
[ 5277.414646] mt7921u 4-2:1.3: WM Firmware Version: ____010000, Build Time: 20250625153703
```
→ **Kernel `mt7921u` successfully loaded firmware on Bus 4 Port 2 (the same bus/port where the test ran).** This is from earlier in the session — the kernel driver already brought the chip up.

```
[ 5379.852575] usb 4-2: USB disconnect, device number 27
[ 5384.860740] usb 2-1: new SuperSpeed USB device number 2 using xhci_hcd
[ 5384.878667] usb 2-1: New USB device found, idVendor=0e8d, idProduct=7961
…
[ 5389.820485] mt7921u 2-1:1.3: HW/SW Version: 0x8a108a10, Build Time: 20250625153620a
[ 5390.070596] mt7921u 2-1:1.3: WM Firmware Version: ____010000, Build Time: 20250625153703
```
→ Device was unplugged from Bus 4, replugged on Bus 2, and **`mt7921u` bound to it again and re-loaded firmware** on Bus 2.

```
[ 5450.627753] mt7921u 2-1:1.3: vendor request req:63 off:d02c failed:-110
[ 5453.828356] mt7921u 2-1:1.3: vendor request req:63 off:d054 failed:-110
[ 5457.028444] mt7921u 2-1:1.3: vendor request req:63 off:d058 failed:-110
[ 5460.228688] mt7921u 2-1:1.3: vendor request req:63 off:53b8 failed:-110
[ 5463.428906] mt7921u 2-1:1.3: vendor request req:63 off:53c4 failed:-110
[ 5466.629184] mt7921u 2-1:1.3: vendor request req:66 off:53c4 failed:-110
[ 5466.785360] mt7921u 2-1:1.3: HW/SW Version: 0x8a108a10, Build Time: 20250625153620a
[ 5466.795667] mt7921u 2-1:1.3: WM Firmware Version: ____010000, Build Time: 20250625153703
[ 5468.910855] wlan1: Driver requested disconnection from AP 00:00:00:00:00:00
```
→ This is **the smoking gun.** A burst of `-110` (`-ETIMEDOUT`) vendor requests from `mt7921u`, every ~3 seconds, then the driver re-initializes the chip *again* (third HW/SW Version line on Bus 2), then `wlan1` requests a disconnect.

`-ETIMEDOUT` from vendor requests, every 3.2 seconds in sequence, exactly looks like the kernel driver fighting our user-space code for the device. The kernel is trying to issue its periodic vendor reads, they time out because we hijacked the chip's USB state, and eventually the kernel does its own recovery (the third HW/SW Version is the kernel re-loading firmware after our test corrupted its session).

**Crucial:** the test ran on **Bus 4 Device 26**, but the dmesg errors are on **Bus 2 Device 2**. Either:
- (a) The device was re-enumerated between the lsusb snapshot and the test run, ending up on Bus 2, and the `lsusb -t` you captured was stale; or
- (b) There were two MT7921AUs / two re-plug events, and the dmesg lines on Bus 2 are unrelated to this test.

The text dump you pasted doesn't include the timestamps of the lsusb snapshots, so I can't pin this down. **First thing to verify when we resume:** correlate the test run's timestamp (`18:51:19.647`) with `dmesg -T` to see which bus/dev the kernel was talking to at that exact moment.

## Probable root cause

The kernel `mt7921u` driver was almost certainly still bound to (or actively re-binding to) the device when the test ran. Evidence:

1. The very first MCU command — the same one verified byte-for-byte against capture-3 — failed at the bulk OUT step, not at the response wait. A driver collision is the cleanest explanation for an OUT to a fresh, "claimed" interface failing immediately.
2. dmesg shows `mt7921u` repeatedly initializing on this device through the session.
3. `lsusb -t` showing `Driver=[none]` for If 3 may have been stale (snapshot taken at a different time than the test) or may reflect the brief window after detach but before the kernel re-grabs the device.
4. The session-pause notes explicitly flagged this risk: *"if `mt7921u` re-grabs the device after you load Wifit3, you'll get permission/claim errors that look like real bugs but aren't."*

What this **does not** rule out:
- A genuine code bug in `transport.py` send path (the snapshot's alternative branch). We can't distinguish driver collision from code bug until we've cleanly isolated the device from the kernel.

## Recommended next steps (for the Windows-land session)

### Step 1 — eliminate the kernel driver collision on Kali, redo the test

The simplest way to get unambiguous Kali results:

```bash
# Blacklist mt7921u so it can't re-bind
echo 'blacklist mt7921u'          | sudo tee /etc/modprobe.d/wifit3.conf
echo 'blacklist mt7921_common'    | sudo tee -a /etc/modprobe.d/wifit3.conf

# Unload everything currently using the chip
sudo rmmod mt7921u mt7921_common mt76_connac_lib mt76_usb mt76 2>/dev/null

# Replug the device. Confirm:
lsusb -t | grep -A1 'Dev .*If 3'   # Should show Driver=[none] for If 3
dmesg | tail -20                    # Should NOT show any mt7921u lines after replug

# Then run the test
sudo .venv/bin/python scratch/test_hw_mt7921au.py
```

Note: `btusb` will still claim Interfaces 0/1/2. That's fine — we only want Interface 3.

If the test **still fails at PATCH_SEM_GET** after a clean blacklist+replug, then we have a real code bug isolated to Kali and Windows-land becomes a longer trip. If it succeeds (or fails later), we've confirmed the collision theory and can move on to whatever it actually fails on.

### Step 2 — capture usbmon during the next test run

You mentioned you grabbed `usbmon2` during this run. Hold onto it but don't trust it as ground truth for "what Wifit3 sent" — it may contain interleaved kernel-driver traffic because of the collision. After Step 1's clean blacklist, capture usbmon again and that one becomes the authoritative trace to diff against `capture-3.pcap`.

```bash
# In another terminal, before running the test:
sudo modprobe usbmon
# Bus number from lsusb -t — pick whichever bus the device is actually on
sudo tshark -i usbmon4 -w /tmp/wifit3-clean.pcapng &
# … run the test …
sudo kill %1
```

### Step 3 — what to do with the existing usbmon2 capture

Even though it's polluted, it's still worth a quick look:

- Filter to the device's bus/address and Interface 3 endpoints only.
- Find the OUT to EP `0x08` that came from our process (timestamp should match `18:51:19.986`). The bytes preceding the failure tell us whether our PATCH_SEM_GET frame actually hit the wire, and if so, what came back (a STALL, a NAK storm, or nothing).
- If the OUT never appears on the wire, the failure was inside libusb / the USB stack before transmission — strongly consistent with the driver-collision theory.
- If the OUT does appear and the device STALL'd the endpoint, the kernel driver had left EP `0x08` in a bad state and we'd need to `clear_halt` on claim.

### Step 4 — Windows side, regardless of Kali outcome

The Windows blockers from the snapshot are still real and unaffected by today's result:

- USB 3.0 / WinUSB **4-packet FW_SCATTER stall** — needs USB 2.0 ports/hubs, or libusb async URB queue investigation.
- Post-FW_START_REQ EP0 dead — the original snapshot blocker.

If Kali works after Step 1, Windows-side hypothesis 1 (WinUSB mishandles firmware-handoff USB event) gets stronger and we focus there. If Kali doesn't work after Step 1, we fix the code bug first and then retest Windows.

## What is still verified (don't re-investigate)

Carrying forward from the 2026-05-17 snapshot — none of this was challenged by today's run:

- All 6 host-to-device firmware-load MCU commands byte-for-byte identical to pcap.
- Interface 3 endpoint map (IN bulk `0x84`/`0x85`, OUT bulk `0x08`/`0x04`/`0x05`/`0x06`/`0x07`/`0x09`, IN intr `0x86`). Confirmed again today by `lsusb -t`.
- Firmware blobs in `assets/` are byte-identical to Kali 6.18.12's `_1_` variant. The successful patch-header parse in today's log (`n_region=1`, `build_date='20250625153620a\n'`) re-confirms this.
- `DL_MODE_NEED_RSP = 0x80000000` (BIT 31).
- Firmware-load command sequence.
- Pre-patch DMA scheduler init.
- MCU responses on EP `0x85` when BIT(31) is set.

## Open questions to resolve

1. Was `mt7921u` actually bound to the device's If 3 at `18:51:19.647` on Kali? (Resolve via `dmesg -T` + lsusb timestamps.)
2. Why did the device migrate Bus 4 → Bus 2 mid-session? Manual replug, or kernel-initiated reset?
3. If Step 1 produces a clean run and the failure recurs at PATCH_SEM_GET, what does the `transport.py` send path look like for cid `0x10` seq `0x01`? Last-known-good is "byte-for-byte matches capture-3 frame 14182" — has anything changed since the 2026-05-17 snapshot was taken? (Check git log; the snapshot didn't enumerate untracked changes.)

## RESOLUTION — 2026-05-19 evening: kernel-driver-collision was correct, but a deeper blocker is now exposed

User ran `scratch/kali_test_mt7921au.sh` (this session's diagnostic
script — installs the blacklist, `rmmod`s the mt76 family, prompts for
replug, runs the test, bundles dmesg/lsusb/usbmon). Two runs in
`usb_dumps/wifit3-kali-bundle/run-20260519T191052Z/` (fresh device) and
`.../run-20260519T191446Z/` (replug after Run 1 wedged the chip).

### Run 1 — kernel-driver-collision theory CONFIRMED

`dmesg_during.txt`: no `mt7921u` lines anywhere during the test window.
`device_sysfs_post_replug.txt` and `device_sysfs_post_test.txt`: both
show `4-2:1.3 -> (none)`. Blacklist+rmmod held perfectly. No bus
migration (`device_migration.txt`: `MIGRATED=no`).

Test progress was dramatically further than the original 2026-05-19
run:

- ✅ PATCH_SEM_GET (cid=0x10 seq=0x01) — **the prior failure point** —
  succeeded, got real response on EP 0x85.
- ✅ PATCH_START_REQ + 23 FW_SCATTER chunks (92 KB patch).
- ✅ PATCH_FINISH_REQ + PATCH_SEM_REL.
- ✅ All 4 WM RAM regions uploaded (89 + 67 + 4 + 13 chunks, ~700 KB).
- ❌ FW_START_REQ bulk OUT (cid=0x02 seq=0x09, EP 0x08): 2-s timeout
  (`[Errno 110] Operation timed out`).
- ❌ Subsequent vendor reads on EP0: ~25 s of `Errno 110` timeouts.
- ❌ dmesg post-test: `usb 4-2: Failed to suspend device, error -110`
  (the kernel itself can't talk to the chip anymore).

So **the original Kali-side question — "is `mt7921u` colliding with our
test?" — gets a clean YES.** Blacklist+rmmod+replug fixes the
PATCH_SEM_GET failure entirely.

### The new wall — and it's the SAME as Windows

Run 1's failure point (FW_START_REQ → EP0 dead) is the same blocker
the 2026-05-17 session pause documented as
"Windows/WinUSB-only". With this Kali run it's now reproduced on
**Kali + libusb + USB-3 SuperSpeed**, so the WinUSB-specific
attribution is wrong. The earlier "Hypothesis #1 — WinUSB mishandles
firmware-handoff USB event" can be retired.

### Run 2 — also reproduces the "4-packet stall"

Run 2 ran ~4 s after replug (Run 1 had ~10 s), starting from a chip
that Run 1 had left in a half-dead state. It failed earlier — at the
first FW_SCATTER chunk, with a **short bulk write of 4096/4104 bytes**.
That is the canonical "Windows 4-packet stall" pattern (see
[MT7921AU.md § "FW_SCATTER 4-packet stall"](./MT7921AU.md#fw_scatter-4-packet-stall-windows--winusb--usb-30))
now reproduced on Linux + libusb. So that's also not WinUSB-specific.

### What's now the leading hypothesis

**Shallow bulk-IN URB pool** (Hypothesis #2 from the
2026-05-17 snapshot, previously a side bet). Linux's kernel driver
pre-submits 128 URBs per IN endpoint via `mt76u_alloc_queues` before
firmware traffic flies; we submit one bulk-IN read at a time on a
drainer thread. The boot ROM appears to use USB-3 flow control across
both directions simultaneously — if we're not in a posted state to
receive an internal ACK or event, the device stops accepting OUTs.

Concrete next code change (deferred — saving as the next session's
agenda):

1. Restructure `transport.py` to use libusb async URB API
   (`libusb_submit_transfer`) instead of sync `Endpoint.read()`.
2. Pre-submit ~32 URBs on EP 0x84 and EP 0x85 before the first MCU
   command, refill on completion.
3. Hw-test on Windows first (faster turnaround, original symptom is
   well-characterised there); if the FW_START_REQ wall comes down,
   confirm on Kali; if not, we re-derive from Linux's pcap how it
   actually sequences URBs around FW_START_REQ.

### Side note: pcap captures lost both runs

`sudo tshark -w "$OUT_DIR/usbmon0.pcap"` returned `Permission denied`
on both runs (see `tshark.log` line 3 in each bundle). Standard Kali
setup: tshark drops to `wireshark` group, can't write into a
`kali:kali` dir. Logs alone were enough to call the failure, but
`scratch/kali_test_mt7921au.sh` should be fixed to either chown the
bundle dir to include the `wireshark` group, or write the pcap to
`/tmp` and move. Worth doing before the next bring-up retest.
