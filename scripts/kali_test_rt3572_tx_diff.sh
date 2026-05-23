#!/usr/bin/env bash
#
# Wifit3 — RT3572 / AWUS051NHv2 TX-RF-silent diff collector
# =========================================================
# Two captures back-to-back for the usbmon diff that should expose the
# missing register write keeping the analog/RF stage silent:
#
#   Phase A — aireplay-ng deauth, kernel rt2800usb driver (working baseline)
#   Phase B — wifit3 deauth (scripts/test_hw.py), kernel modules unloaded
#
# Outputs both pcaps to usb_dumps/captures_rt3572_tx_diff/ ; ship that
# folder back to the dev box. Hardcoded target BSSID + client + iface
# match scripts/test_hw.py + src/wifit3/scripts/capture.py — change them
# all together if your test setup changes.
#
# REQUIRES: Kali, sudo, RT3572 plugged in, `uv sync` already run.

set -u

# ---- config ------------------------------------------------------------
TARGET_BSSID="aa:bb:cc:dd:ee:01"   # user's AP   (matches test_hw.py:50)
CLIENT_BSSID="04:2E:C1:51:43:B8"   # user's phone (matches test_hw.py:50)
CHANNEL=1
BASE_IFACE="wlan1"                 # matches capture.py:54
USBMON="usbmon3"                   # matches capture.py:53
VID_PID="148f:3572"                # RT3572 / AWUS051NHv2
CAP_DURATION=20                    # seconds of TX per phase

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="$REPO_ROOT/usb_dumps/captures_rt3572_tx_diff"
VENV_PY="$REPO_ROOT/.venv/bin/python"
mkdir -p "$OUTDIR"

# ---- prereqs -----------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "[FATAL] missing: $1"; exit 1; }; }
need aireplay-ng
need airmon-ng
need tshark
need iw
need lsusb
[[ -x "$VENV_PY" ]] || { echo "[FATAL] $VENV_PY missing — run 'uv sync'"; exit 1; }

lsusb | grep -qi "$VID_PID" || { echo "[FATAL] RT3572 ($VID_PID) not detected — plug it in"; exit 1; }

sudo -v
sudo modprobe usbmon

# ---- helpers -----------------------------------------------------------
TSHARK_PID=""
start_tshark() {
    sudo tshark -i "$USBMON" -w "$1" -q >"$OUTDIR/tshark.log" 2>&1 &
    TSHARK_PID=$!
    sleep 1   # let tshark actually open the iface
}
stop_tshark() {
    sleep 1   # flush
    [[ -n "$TSHARK_PID" ]] && sudo kill "$TSHARK_PID" 2>/dev/null || true
    wait "$TSHARK_PID" 2>/dev/null || true
    TSHARK_PID=""
}

cleanup() {
    [[ -n "$TSHARK_PID" ]] && sudo kill "$TSHARK_PID" 2>/dev/null || true
    sudo airmon-ng stop "${BASE_IFACE}mon" >/dev/null 2>&1 || true
    sudo airmon-ng stop "$BASE_IFACE"      >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---- PHASE A: aireplay-ng (kernel driver baseline) ---------------------
echo "=== PHASE A: aireplay-ng deauth (kernel rt2800usb, working baseline) ==="
sudo modprobe rt2800usb 2>/dev/null || true
sudo airmon-ng check kill >/dev/null
sleep 2
sudo airmon-ng start "$BASE_IFACE" >/dev/null

# airmon-ng on modern kernels usually creates wlan1mon; some leave wlan1.
MON_IFACE="${BASE_IFACE}mon"
[[ -d "/sys/class/net/$MON_IFACE" ]] || MON_IFACE="$BASE_IFACE"
sudo iw dev "$MON_IFACE" set channel "$CHANNEL"

start_tshark "$OUTDIR/aireplay.pcap"
echo "[*] aireplay-ng -0 0 -a $TARGET_BSSID -c $CLIENT_BSSID $MON_IFACE  (${CAP_DURATION}s)"
sudo timeout "$CAP_DURATION" aireplay-ng -0 0 -a "$TARGET_BSSID" -c "$CLIENT_BSSID" "$MON_IFACE" \
    > "$OUTDIR/aireplay.log" 2>&1 || true
stop_tshark
sudo airmon-ng stop "$MON_IFACE" >/dev/null 2>&1 || true

# ---- PHASE B: wifit3 (kernel modules unloaded, failing case) -----------
echo
echo "=== PHASE B: wifit3 deauth via scripts/test_hw.py (kernel rt2x00 unloaded) ==="
sudo rmmod rt2800usb rt2x00usb rt2x00lib 2>/dev/null || true
sleep 1
lsusb | grep -qi "$VID_PID" || { echo "[FATAL] device disappeared after rmmod"; exit 1; }

start_tshark "$OUTDIR/wifit3.pcap"
echo "[*] uv run scripts/test_hw.py --debug  (will run ~20s including 15s observe window)"
( cd "$REPO_ROOT" && sudo "$VENV_PY" scripts/test_hw.py --debug ) \
    > "$OUTDIR/wifit3_test_hw.log" 2>&1 || true
stop_tshark

# ---- chown so the user can read everything -----------------------------
sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$OUTDIR" 2>/dev/null || true

# ---- summary -----------------------------------------------------------
echo
echo "============================================================"
echo "  Captures saved to: $OUTDIR"
ls -l "$OUTDIR"
echo
echo "  Ship the whole folder back:"
echo "    scp -r $OUTDIR user@windows-host:/path/"
echo "============================================================"
