#!/usr/bin/env bash
#
# Wifit3 — RT3572 / AWUS051NHv2 TX-RF-silent diff collector
# =========================================================
# Captures two back-to-back usbmon traces so we can diff the USB register
# writes and find what keeps the RT3572 analog/RF stage silent on TX:
#
#   Phase A — aireplay-ng deauth on the *kernel* rt2800usb driver   (WORKS = baseline)
#   Phase B — wifit3 deauth via scripts/test_hw.py, rt2x00 unloaded (FAILS = under test)
#
# Both pcaps land in usb_dumps/captures_rt3572_tx_diff/ — ship the whole
# folder to the dev box and diff with scripts/pcap_slicer.py et al.
#
# ── BEFORE YOU RUN ──────────────────────────────────────────────────────
#   1. Plug in the RT3572 (148f:3572 / AWUS051NHv2).
#   2. Load + bind the kernel driver:   sudo modprobe rt2800usb
#      (re-plug the card afterwards if no wlanN interface shows up).
#   3. `uv sync` must have been run (needs .venv/bin/python).
#   4. Run from anywhere:   ./scripts/kali_test_rt3572_tx_diff.sh
#
#   The HARD PREFLIGHT below STOPS with a fix-it message if any of the above
#   is missing — no more half-broken runs.
#
# ── WHAT IT DOES TO YOUR SYSTEM (no surprises) ──────────────────────────
#   • runs `airmon-ng check kill`  → this KILLS NetworkManager (your normal
#     Wi-Fi drops). It is NOT restarted automatically; run
#         sudo systemctl start NetworkManager
#     yourself when you want normal Wi-Fi back.
#   • BOTH phases prompt you to PHYSICALLY UNPLUG, then RE-PLUG the card (same
#     USB port each time!) so usbmon captures each cold-boot init from scratch.
#   • puts the card into monitor mode (wlanNmon) for Phase A.
#   • `rmmod`s the rt2x00 stack for Phase B (so libusb gets the card cold),
#     then RELOADS rt2800usb on exit so the card always comes back. Re-plug
#     if Linux doesn't re-enumerate it.
#
# Tunables (BSSID / client / channel) must match scripts/test_hw.py — change
# them together.
#
# REQUIRES: Kali, sudo, RT3572 plugged in, `uv sync` already run.

set -u

# ---- config ------------------------------------------------------------
TARGET_BSSID="aa:bb:cc:dd:ee:01"   # user's AP    (matches test_hw.py:50)
CLIENT_BSSID="04:2E:C1:51:43:B8"   # user's phone (matches test_hw.py:50)
CHANNEL=1                          # matches test_hw.py:39
BASE_IFACE="wlan1"                 # default; auto-detected from the bound driver below
USBMON="usbmon3"                   # default; auto-synced to the card's USB bus below
VID_PID="148f:3572"                # RT3572 / AWUS051NHv2
CAP_DURATION=20                    # seconds of TX for Phase A (aireplay)

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="$REPO_ROOT/usb_dumps/captures_rt3572_tx_diff"
VENV_PY="$REPO_ROOT/.venv/bin/python"
mkdir -p "$OUTDIR"

MON_IFACE=""
CAP_PID=""

say()  { echo; echo "=== $* ==="; }
info() { echo "    $*"; }
die()  { echo; echo "[FATAL] $*" >&2; exit 1; }

# ---- driver / bus auto-detection ---------------------------------------
# Find the wlanN interface whose USB device is bound to rt2800usb (the RT3572).
detect_rt_iface() {
    local net drv
    for net in /sys/class/net/*; do
        [[ -e "$net/device/driver" ]] || continue
        drv=$(basename "$(readlink -f "$net/device/driver")")
        [[ "$drv" == "rt2800usb" ]] && { basename "$net"; return 0; }
    done
    return 1
}

# ---- capture helpers ---------------------------------------------------
# AppArmor confines dumpcap's OWN file open, so `dumpcap -w <file>` fails with
# "Permission denied" even under sudo. Workaround: dumpcap writes the pcap to
# stdout (-w -) and the unprivileged shell redirect (> file) creates the file
# as $USER, which is unconfined. (Verified: yields a valid pcapng.)
start_cap() {   # $1 = output pcap, $2 = short tag for the per-phase log
    sudo dumpcap -i "$USBMON" -w - -q 2>"$OUTDIR/dumpcap-$2.log" > "$1" &
    CAP_PID=$!
    sleep 1.5   # let dumpcap actually open usbmon before TX starts
    info "capturing $USBMON -> $1"
}
stop_cap() {
    sleep 1     # let the last frames land in usbmon
    sudo pkill -INT -f "dumpcap -i $USBMON" 2>/dev/null || true
    wait "$CAP_PID" 2>/dev/null || true
    CAP_PID=""
}

# ---- interactive / wait helpers ----------------------------------------
press_enter() { read -r -p "    >>> $1" _ </dev/tty; }

# Poll until rt2800usb binds a wlanN interface (e.g. after a physical re-plug).
wait_for_iface() {   # $1 = timeout seconds (default 25)
    local deadline=$(( SECONDS + ${1:-25} )) d
    while (( SECONDS < deadline )); do
        if d=$(detect_rt_iface); then echo "$d"; return 0; fi
        sleep 0.5
    done
    return 1
}

# Poll until the RT3572 VID:PID is on the USB bus (no kernel driver needed).
wait_for_usb() {   # $1 = timeout seconds (default 25)
    local deadline=$(( SECONDS + ${1:-25} ))
    while (( SECONDS < deadline )); do
        lsusb | grep -qi "$VID_PID" && return 0
        sleep 0.5
    done
    return 1
}

# Echo the USB bus number the RT3572 currently sits on (via sysfs), else nothing.
rt_usb_bus() {
    local d want_vid="${VID_PID%%:*}" want_pid="${VID_PID##*:}"
    for d in /sys/bus/usb/devices/*/idProduct; do
        [[ -r "$d" ]] || continue
        [[ "$(<"$d")" == "$want_pid" && "$(<"$(dirname "$d")/idVendor")" == "$want_vid" ]] \
            && { cat "$(dirname "$d")/busnum" 2>/dev/null; return 0; }
    done
    return 1
}

# ---- cleanup (always restores the card) --------------------------------
cleanup() {
    [[ -n "${CAP_PID:-}" ]] && sudo pkill -INT -f "dumpcap -i $USBMON" 2>/dev/null || true
    [[ -n "${MON_IFACE:-}" ]] && sudo airmon-ng stop "$MON_IFACE" >/dev/null 2>&1 || true
    # Always reload the kernel driver so the card comes back for the user.
    sudo modprobe rt2800usb 2>/dev/null || true
    echo
    echo "[i] Cleanup done: rt2800usb reloaded. NetworkManager was NOT restarted"
    echo "    (killed by 'airmon-ng check kill'). For normal Wi-Fi back:"
    echo "        sudo systemctl start NetworkManager"
    echo "    If 'wlanN' didn't reappear, unplug/replug the RT3572."
}
trap cleanup EXIT

# ---- HARD PREFLIGHT ----------------------------------------------------
say "PREFLIGHT — verifying the card is ready (no half-broken runs)"

need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }
need aireplay-ng; need airmon-ng; need dumpcap; need iw; need lsusb; need modprobe
[[ -x "$VENV_PY" ]] || die "$VENV_PY missing — run 'uv sync' in $REPO_ROOT first"

sudo -v || die "this script needs sudo"
sudo modprobe usbmon || die "could not load the usbmon kernel module"

lsusb | grep -qi "$VID_PID" \
    || die "RT3572 ($VID_PID) not on the USB bus — plug it in, then rerun."
info "RT3572 present on USB bus ($VID_PID)."

lsmod | grep -q '^rt2800usb' \
    || die $'rt2800usb kernel driver not loaded.\n        Fix:  sudo modprobe rt2800usb   (then re-plug the card if no wlanN appears)'
info "rt2800usb kernel driver loaded."

if det=$(detect_rt_iface); then
    BASE_IFACE="$det"
    info "RT3572 bound to interface: $BASE_IFACE"
else
    die $'RT3572 is on the bus but NOT bound to a wlanN interface.\n        A previous run probably left the driver unloaded, or the card needs a re-plug.\n        Fix:  sudo modprobe rt2800usb  &&  unplug/replug the card, then rerun.'
fi

# Auto-sync the usbmon node to the bus the card actually sits on.
ifacedev=$(readlink -f "/sys/class/net/$BASE_IFACE/device" 2>/dev/null)
busnum=$(cat "$(dirname "$ifacedev")/busnum" 2>/dev/null || true)
if [[ -n "${busnum:-}" ]]; then
    USBMON="usbmon$busnum"
fi
info "Using usbmon interface: $USBMON   (the card's USB bus)"
info "Target AP: $TARGET_BSSID    Client: $CLIENT_BSSID    Channel: $CHANNEL"
info "Output dir: $OUTDIR"

# ---- PHASE A: cold-boot kernel rt2800usb baseline ----------------------
# Capture opens BEFORE the card is on the bus, so usbmon records everything
# "from the moment it touches the metal": USB enumeration (descriptor reads,
# SET_CONFIG) -> rt2800usb probe -> firmware upload -> RF/BBP init ->
# monitor bring-up -> channel tune -> TX. This is what makes Phase A
# symmetric with Phase B's full wifit3 bring-up, so the diff can expose the
# RF-enable write wifit3 is missing.
say "PHASE A — kernel rt2800usb COLD-BOOT + aireplay deauth (WORKING baseline)"
info "About to run 'airmon-ng check kill' — KILLS NetworkManager so it can't"
info "grab the card when you re-plug it."
sudo airmon-ng check kill >/dev/null
sleep 1

# --- open the capture while the card is OFF the bus ---
echo
echo "    ACTION 1 of 2: physically UNPLUG the RT3572 now."
press_enter "Press ENTER once the card is REMOVED... "
sudo -v   # refresh sudo creds — the prompts can sit a while
start_cap "$OUTDIR/aireplay.pcap" aireplay
echo
echo "    ACTION 2 of 2: now PLUG the RT3572 back into the SAME USB port."
press_enter "Press ENTER once the card is INSERTED... "

info "Waiting for the kernel to enumerate + bind rt2800usb ..."
if det=$(wait_for_iface 30); then
    BASE_IFACE="$det"
    info "Card came back as: $BASE_IFACE"
else
    die "card did not re-bind to rt2800usb within 30s — check the cable/port and rerun."
fi

# Guard: same physical port == same USB bus == the usbmon node we're capturing.
newdev=$(readlink -f "/sys/class/net/$BASE_IFACE/device" 2>/dev/null)
newbus=$(cat "$(dirname "$newdev")/busnum" 2>/dev/null || true)
if [[ -n "${newbus:-}" && "usbmon$newbus" != "$USBMON" ]]; then
    info "WARNING: card returned on bus $newbus but we are capturing $USBMON."
    info "         You used a DIFFERENT USB port — the cold-boot frames were MISSED."
    info "         Ctrl-C, re-run, and re-plug into the SAME port for a complete trace."
fi

sleep 2   # let firmware load / device settle before monitor mode
info "Starting monitor mode on $BASE_IFACE ..."
sudo airmon-ng start "$BASE_IFACE" >/dev/null

# airmon-ng usually creates wlanNmon; some setups keep wlanN.
MON_IFACE="${BASE_IFACE}mon"
[[ -d "/sys/class/net/$MON_IFACE" ]] || MON_IFACE="$BASE_IFACE"
info "Monitor interface: $MON_IFACE"

sudo iw dev "$MON_IFACE" set channel "$CHANNEL" \
    || die "failed to set channel $CHANNEL on $MON_IFACE"
nowch=$(iw dev "$MON_IFACE" info 2>/dev/null | grep -i channel | tr -s ' \t' ' ')
info "channel ->${nowch:- (could not read back)}"

info "running: aireplay-ng -0 0 -a $TARGET_BSSID -c $CLIENT_BSSID $MON_IFACE   (${CAP_DURATION}s)"
sudo timeout "$CAP_DURATION" aireplay-ng -0 0 -a "$TARGET_BSSID" -c "$CLIENT_BSSID" "$MON_IFACE" \
    > "$OUTDIR/aireplay.log" 2>&1 || true
stop_cap
sudo airmon-ng stop "$MON_IFACE" >/dev/null 2>&1 || true
MON_IFACE=""
info "Phase A capture saved: $OUTDIR/aireplay.pcap"

# ---- PHASE B: wifit3 COLD-BOOT (kernel rt2x00 unloaded, failing case) ---
# Symmetric with Phase A: we unload the kernel stack FIRST so nothing binds
# the card on re-plug, open the capture while it's off the bus, then let you
# re-plug. usbmon then records wifit3's cold boot from the same starting line
# as the kernel's: USB enumeration -> libusb claim -> wifit3 connect/bring-up
# -> set channel -> deauth TX. No wlanN appears (no kernel driver), so we
# wait on the USB device, not a net interface.
say "PHASE B — wifit3 COLD-BOOT via scripts/test_hw.py (kernel rt2x00 UNLOADED)"
info "Unloading rt2x00 stack so NOTHING in the kernel claims the card on re-plug ..."
sudo rmmod rt2800usb rt2x00usb rt2x00lib 2>/dev/null || true
sleep 1

# --- open the capture while the card is OFF the bus ---
echo
echo "    ACTION 1 of 2: physically UNPLUG the RT3572 now."
press_enter "Press ENTER once the card is REMOVED... "
sudo -v   # refresh sudo creds — the prompts can sit a while
start_cap "$OUTDIR/wifit3.pcap" wifit3
echo
echo "    ACTION 2 of 2: now PLUG the RT3572 back into the SAME USB port."
press_enter "Press ENTER once the card is INSERTED... "

info "Waiting for the RT3572 to enumerate on USB (no kernel driver will bind) ..."
wait_for_usb 30 || die "RT3572 ($VID_PID) did not appear on USB within 30s — check the cable/port and rerun."
info "RT3572 present on USB bus."

# Guard: same physical port == same USB bus == the usbmon node we're capturing.
newbus=$(rt_usb_bus || true)
if [[ -n "${newbus:-}" && "usbmon$newbus" != "$USBMON" ]]; then
    info "WARNING: card returned on bus $newbus but we are capturing $USBMON."
    info "         You used a DIFFERENT USB port — the cold-boot frames were MISSED."
    info "         Ctrl-C, re-run, and re-plug into the SAME port for a complete trace."
fi
sleep 1   # let enumeration settle before libusb claims it

info "running: $VENV_PY scripts/test_hw.py --debug"
info "  test_hw: discover -> connect/bring-up -> set ch1 -> deauth -> 15s observe -> close (~25-35s)"
( cd "$REPO_ROOT" && sudo "$VENV_PY" scripts/test_hw.py --debug ) \
    > "$OUTDIR/wifit3_test_hw.log" 2>&1 || true
stop_cap
info "Phase B capture saved: $OUTDIR/wifit3.pcap"

# ---- summary -----------------------------------------------------------
say "DONE"
sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$OUTDIR" 2>/dev/null || true
ls -l "$OUTDIR"
echo
for f in aireplay wifit3; do
    p="$OUTDIR/$f.pcap"
    if [[ -s "$p" ]]; then
        info "OK   $f.pcap ($(du -h "$p" | cut -f1)) — non-empty"
    else
        echo "    WARN $f.pcap is EMPTY/missing — capture failed (see dumpcap-$f.log)"
    fi
done
echo
echo "Ship the whole folder to the dev box:"
echo "    scp -r $OUTDIR user@dev-box:/path/"
echo "============================================================"
