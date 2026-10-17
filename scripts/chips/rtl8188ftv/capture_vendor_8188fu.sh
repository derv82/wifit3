#!/usr/bin/env bash
#
# Vendor (DKMS) cold-boot capture for RTL8188FTV 0bda:f179.
#
# Builds + installs kelebek333/rtl8188fu at the pinned commit with monitor mode
# enabled, binds it, then runs wifit3's automated usbmon capture
# (src/wifit3/scripts/capture.py) so the DKMS re-port has a pcap +
# driver-source bundle to port against.
#
# Run as root on the Linux capture box, with the card UNPLUGGED:
#   sudo bash scripts/chips/rtl8188ftv/capture_vendor_8188fu.sh \
#     --bssid2g <AP_BSSID_CH1> [--client2g <CLIENT>] [--channel2g 1]
#
# Flags:
#   --bssid2g BSSID    2.4 GHz AP for the raw injection test (recommended:
#                      without it the capture has no TX tail to byte-match).
#   --channel2g N      channel for the --bssid2g pass (default 1).
#   --client2g BSSID   client for the --bssid2g deauth.
#   --sta-ssid SSID    OPEN AP for the station TX phase (associate + DHCP +
#                      ping + disconnect): DATA + deauth MGMT TX reference for
#                      drivers whose monitor TX never reaches USB.
#   --sta-pings N      gateway ping count in the station phase (default 20).
#   --iface NAME       use this existing netdev, skip the plug wait (warm
#                      reference: card already plugged from a previous run,
#                      no replug).
#   --src-dir DIR      vendor source checkout (default /usr/src/rtl8188fu-1.0).
#   --skip-build       skip the DKMS build/install (source already installed).
#   --skip-capture     stop after driver install + verification, no capture.
#
set -euo pipefail

PIN_COMMIT="799fa0beacadce84bef6514d5948b454a8037c96"
REPO_URL="https://github.com/kelebek333/rtl8188fu"
SRC_DIR="/usr/src/rtl8188fu-1.0"
SKIP_BUILD=0
SKIP_CAPTURE=0
BSSID2G=""
CLIENT2G=""
CHANNEL2G="1"
STASSID=""
STAPINGS="20"
IFACE=""

usage() {
    sed -n '2,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bssid2g)   BSSID2G="${2:-}"; shift 2 ;;
        --client2g)  CLIENT2G="${2:-}"; shift 2 ;;
        --channel2g) CHANNEL2G="${2:-}"; shift 2 ;;
        --sta-ssid)  STASSID="${2:-}"; shift 2 ;;
        --sta-pings) STAPINGS="${2:-}"; shift 2 ;;
        --iface)     IFACE="${2:-}"; shift 2 ;;
        --src-dir)   SRC_DIR="${2:-}"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --skip-capture) SKIP_CAPTURE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown flag $1 (see --help)" >&2; exit 2 ;;
    esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "error: must run as root  ->  sudo bash $0 --bssid2g <BSSID>" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CAPTURE_PY="$REPO_ROOT/src/wifit3/scripts/capture.py"
if [[ ! -f "$CAPTURE_PY" ]]; then
    echo "error: capture tool not found at $CAPTURE_PY" >&2
    exit 1
fi

echo "=== 0. prereqs ==="
for cmd in git dkms tshark airodump-ng iw ethtool modprobe modinfo lsusb; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "error: missing $cmd" >&2; exit 1; }
done
if [[ ! -d "/lib/modules/$(uname -r)/build" ]]; then
    echo "error: linux-headers for $(uname -r) not installed" >&2
    echo "  sudo apt install -y linux-headers-$(uname -r)" >&2
    exit 1
fi
modprobe usbmon
echo "[ok] prereqs present, kernel $(uname -r)"

if [[ "$SKIP_BUILD" -eq 0 ]]; then
    echo "=== 1. vendor source @ $PIN_COMMIT ==="
    if [[ ! -d "$SRC_DIR/.git" ]]; then
        git clone "$REPO_URL" "$SRC_DIR"
    fi
    git -C "$SRC_DIR" fetch origin
    git -C "$SRC_DIR" checkout "$PIN_COMMIT"
    echo "[ok] source: $(git -C "$SRC_DIR" rev-parse HEAD) $(git -C "$SRC_DIR" log -1 --format='%ad %s' --date=short)"

    echo "=== 2. monitor + no-powersave patch ==="
    sed -i 's/^CONFIG_POWER_SAVING = y/CONFIG_POWER_SAVING = n/' "$SRC_DIR/Makefile"
    sed -i 's/^CONFIG_WIFI_MONITOR = n/CONFIG_WIFI_MONITOR = y/' "$SRC_DIR/Makefile"
    grep -E '^(CONFIG_POWER_SAVING|CONFIG_WIFI_MONITOR|CONFIG_RTL8188F|CONFIG_USB_HCI)' "$SRC_DIR/Makefile"
    grep -q '^CONFIG_POWER_SAVING = n' "$SRC_DIR/Makefile"
    grep -q '^CONFIG_WIFI_MONITOR = y' "$SRC_DIR/Makefile"
    echo "[ok] Makefile patched"

    echo "=== 3. blacklist mainline competitors ==="
    echo -e 'blacklist rtl8xxxu\nblacklist r8188eu' > /etc/modprobe.d/rtl8188fu-blacklist.conf
    modprobe -r rtl8xxxu r8188eu 2>/dev/null || true
    echo "[ok] blacklist written"

    echo "=== 4. dkms build + install ==="
    dkms remove rtl8188fu/1.0 --all 2>/dev/null || true
    dkms add rtl8188fu/1.0 2>/dev/null || true
    dkms build rtl8188fu/1.0
    dkms install rtl8188fu/1.0
    mkdir -p /lib/firmware/rtlwifi
    cp -n "$SRC_DIR/firmware/rtl8188fufw.bin" /lib/firmware/rtlwifi/ || true
    echo 'options rtl8188fu rtw_power_mgnt=0 rtw_enusbss=0' > /etc/modprobe.d/rtl8188fu.conf
    echo "[ok] dkms installed"
else
    echo "=== 1-4. skipped (--skip-build) ==="
fi

echo "=== 5. bind + verify ==="
modprobe -r rtl8xxxu r8188eu 2>/dev/null || true
modprobe rtl8188fu
modinfo rtl8188fu | grep -E '^(filename|version|srcversion|vermagic|firmware)'
dkms status | grep 8188fu || { echo "error: dkms status has no 8188fu row" >&2; exit 1; }
if lsmod | grep -q '^rtl8xxxu '; then
    echo "error: rtl8xxxu is still bound; fix the blacklist and retry" >&2
    exit 1
fi
lsmod | grep -E '8188|rtl8xxxu' || true
echo "[ok] rtl8188fu bound"

if [[ "$SKIP_CAPTURE" -eq 1 ]]; then
    echo "[done] driver ready, capture skipped. Re-run without --skip-capture next."
    exit 0
fi

if [[ -n "$IFACE" ]]; then
    echo "=== 6. warm-reference capture (card stays PLUGGED on $IFACE) ==="
else
    echo "=== 6. cold-boot capture (card must be UNPLUGGED now) ==="
fi
if [[ -z "$BSSID2G" ]]; then
    echo "[!] no --bssid2g: capture will have NO injection/TX tail (byte-match gap)."
fi
CAP_ARGS=()
[[ -n "$BSSID2G" ]] && CAP_ARGS+=(--bssid2g "$BSSID2G" --channel2g "$CHANNEL2G")
[[ -n "$CLIENT2G" ]] && CAP_ARGS+=(--client2g "$CLIENT2G")
[[ -n "$STASSID" ]] && CAP_ARGS+=(--station-ssid "$STASSID" --station-pings "$STAPINGS")
[[ -n "$IFACE" ]] && CAP_ARGS+=(--iface "$IFACE")
# Raw injector, not aireplay: rtw_monitor_xmit_entry drops any injected frame
# whose radiotap header is not exactly 12 bytes (silently), so aireplay's
# probes never reach USB (0 bulk-OUT in captures 1-4). The raw path emits
# deauth + directed probes with a 12-byte radiotap over AF_PACKET.
CAP_ARGS+=(--tx-injector raw)
if [[ "${#CAP_ARGS[@]}" -gt 0 ]]; then
    python3 "$CAPTURE_PY" "${CAP_ARGS[@]}"
else
    python3 "$CAPTURE_PY"
fi

echo "=== 7. bundle check ==="
# capture.py saves next to itself (src/wifit3/scripts/captures_<chip>/); the
# canonical home is driver_captures/ (gitignored). Accept either, prefer the
# newest pcap overall.
NEWEST_PCAP=$(ls -t "$REPO_ROOT"/src/wifit3/scripts/captures_*/capture-*.pcap \
    "$REPO_ROOT"/driver_captures/captures_*/capture-*.pcap 2>/dev/null | head -1 || true)
if [[ -z "$NEWEST_PCAP" ]]; then
    echo "error: no capture-*.pcap found under src/wifit3/scripts/captures_*/ or driver_captures/captures_*/" >&2
    exit 1
fi
BUNDLE_DIR="$(dirname "$NEWEST_PCAP")"
NEWEST_LOG=$(ls -dt "$BUNDLE_DIR"/capture-*_logs 2>/dev/null | head -1 || true)
if [[ -z "$NEWEST_LOG" ]]; then
    echo "error: no capture-*_logs next to $NEWEST_PCAP" >&2
    exit 1
fi
echo "newest bundle: $NEWEST_PCAP"
echo "newest logs:   $NEWEST_LOG"
grep -H 'modinfo rtl8188fu' "$NEWEST_LOG/driver.log" | head -2
grep -c 'Running: sudo iw dev .* set channel' "$NEWEST_LOG/main.log"
ls "$BUNDLE_DIR/driver-source/dkms.conf" 2>/dev/null || echo "[!] driver-source/ missing (DKMS autodetect failed; copy $SRC_DIR manually)"
ls "$BUNDLE_DIR/firmware/rtl8188fufw.bin" 2>/dev/null || echo "[!] firmware blob missing"
# The plug prefix must be present: the card's enumeration (standard, non-0x05
# control frames for its device number) proves tshark was dumping before the
# plug. Zero means the start was missed (tshark still spinning up) — re-run.
DEVNUM=$(grep -oP 'Device \K[0-9]+(?=: ID 0bda:f179)' "$NEWEST_LOG/usb-topology.log" | head -1)
if [[ -n "$DEVNUM" ]]; then
    NONVENDOR=$(tshark -r "$NEWEST_PCAP" -Y "usb.device_address==$DEVNUM" \
        -T fields -e usb.urb_type -e usb.setup.bRequest 2>/dev/null \
        | awk '$1=="\047S\047" && $2!="" && $2!=5{c++} END{print c+0}')
    echo "enumeration (non-vendor) frames for dev $DEVNUM: $NONVENDOR"
    if [[ "$NONVENDOR" -eq 0 ]]; then
        echo "[!] NO enumeration frames: the capture missed the plug prefix."
        echo "    Re-run with the card unplugged (sudo bash $0 --skip-build ...)."
    fi
fi
echo
echo "[done] paste this back to the agent:"
echo "  bundle: $NEWEST_PCAP"
echo "  logs:   $NEWEST_LOG"
echo "  source: $(git -C "$SRC_DIR" rev-parse HEAD)"
grep -E 'vermagic|firmware:' "$NEWEST_LOG/driver.log" | head -5
