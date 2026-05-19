#!/usr/bin/env bash
#
# Wifit3 — MT7921AU Kali test-run collector
# =========================================
#
# Runs scratch/test_hw_mt7921au.py under controlled conditions on Kali and
# bundles every artefact we need to diagnose the bring-up failure, so we don't
# have to keep rebooting back into Kali. Targets the open questions in
# src/wifit3/chips/mt7921au/KALI-HANDOFF-2026-05-19.md:
#
#   1. Did the kernel mt7921u driver actually re-bind during the test?
#   2. Did our PATCH_SEM_GET bulk OUT hit the wire, or fail inside libusb?
#   3. Why did the device migrate buses mid-session last time?
#
# Companion to scratch/kali_dump.sh (static info dump) — this one is the
# dynamic test-run + usbmon trace.
#
# REQUIRES: a wifit3 checkout on Kali with `uv sync` already run (so
# .venv/bin/python exists). MT7921AU plugged in, target user has sudo.
#
# USAGE
#   ./scratch/kali_test_mt7921au.sh [output-base-dir]
#
#   default base dir: ~/wifit3-kali-bundle
#   final tarball:    <base>/wifit3-mt7921au-<UTC-TS>.tar.gz
#
# The script will prompt once for a replug after rmmod-ing the mt76 family,
# otherwise it runs end-to-end unattended. Total wall-clock ~30s.
# ----------------------------------------------------------------------------

set -u
set -o pipefail

VID=0e8d
PID=7961
TARGET_DESC="MT7921AU ($VID:$PID)"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_SCRIPT="$REPO_ROOT/scratch/test_hw_mt7921au.py"
VENV_PY="$REPO_ROOT/.venv/bin/python"

OUT_BASE="${1:-$HOME/wifit3-kali-bundle}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$OUT_BASE/run-$TS"
mkdir -p "$OUT_DIR"

LOG="$OUT_DIR/script.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== Wifit3 MT7921AU Kali Test Collector — $TS ==="
echo "Repo root: $REPO_ROOT"
echo "Output:    $OUT_DIR"
echo

# ---- prereqs -----------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "[FATAL] missing tool: $1"; exit 1; }; }
need lsusb
need dmesg
need tshark
need sudo
need tar
need awk
need grep
need sed

[[ -f "$TEST_SCRIPT" ]] || { echo "[FATAL] $TEST_SCRIPT not found"; exit 1; }
[[ -x "$VENV_PY"     ]] || { echo "[FATAL] $VENV_PY missing — run 'uv sync' first"; exit 1; }

echo "[*] Requesting sudo (caching for the rest of the run)..."
sudo -v
( while true; do sudo -n true 2>/dev/null; sleep 30; done ) &
SUDO_KEEPALIVE_PID=$!

cleanup() {
    [[ -n "${TSHARK_PID:-}" ]]          && sudo kill "$TSHARK_PID" 2>/dev/null || true
    [[ -n "${DMESG_PID:-}"  ]]          && sudo kill "$DMESG_PID"  2>/dev/null || true
    [[ -n "${SUDO_KEEPALIVE_PID:-}" ]]  && kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---- pre-state snapshot ------------------------------------------------------
echo "[*] Recording pre-test state..."
{
    echo "# date -u";          date -u
    echo "# uname -a";          uname -a
    echo "# /etc/os-release";   cat /etc/os-release 2>/dev/null
    echo "# tshark --version";  tshark --version | head -3
    echo "# /proc/cmdline";     cat /proc/cmdline
} > "$OUT_DIR/meta.txt"

lsusb                                                      > "$OUT_DIR/lsusb_pre.txt"
lsusb -t                                                   > "$OUT_DIR/lsusb_tree_pre.txt"
sudo lsusb -v -d "$VID:$PID"                               > "$OUT_DIR/lsusb_verbose_pre.txt" 2>&1 || true
lsmod                                                      > "$OUT_DIR/lsmod_pre.txt"
lsmod | grep -E '^(mt76|mt7921)'                           > "$OUT_DIR/lsmod_mt76_pre.txt" || true
sudo dmesg -T                                              > "$OUT_DIR/dmesg_pre.txt" 2>&1
sudo cat /sys/kernel/debug/usb/devices                     > "$OUT_DIR/usb_devices_pre.txt" 2>/dev/null || true
cat /etc/modprobe.d/wifit3.conf 2>/dev/null \
    || echo "(no /etc/modprobe.d/wifit3.conf)"             > "$OUT_DIR/modprobe_blacklist_pre.txt"

# ---- blacklist + rmmod (recipe straight from KALI-HANDOFF-2026-05-19.md) -----
echo "[*] Installing modprobe blacklist (idempotent)..."
sudo tee /etc/modprobe.d/wifit3.conf >/dev/null <<'EOF'
# Wifit3 — block kernel mt76 family so userspace driver owns the device
blacklist mt7921u
blacklist mt7921_common
EOF

echo "[*] Unloading mt76 family modules (skipping any not loaded)..."
for m in mt7921u mt7921_common mt76_connac_lib mt76_usb mt76; do
    if lsmod | awk '{print $1}' | grep -qx "$m"; then
        echo "    rmmod $m"
        sudo rmmod "$m" 2>&1 || echo "    [WARN] rmmod $m failed (still in use?)"
    else
        echo "    $m not loaded"
    fi
done

lsmod | grep -E '^(mt76|mt7921)' > "$OUT_DIR/lsmod_mt76_after_rmmod.txt" \
    || echo "(no mt76/mt7921 modules loaded)" > "$OUT_DIR/lsmod_mt76_after_rmmod.txt"

# ---- usbmon ------------------------------------------------------------------
echo "[*] Loading usbmon..."
sudo modprobe usbmon
ls /sys/kernel/debug/usb/usbmon/ > "$OUT_DIR/usbmon_interfaces.txt" 2>/dev/null || true

# ---- replug ------------------------------------------------------------------
cat <<EOF

============================================================
  ACTION REQUIRED
  ----------------
  1. UNPLUG the MT7921AU.
  2. Wait ~3 seconds.
  3. PLUG it back in. USB 3.0 SuperSpeed port is preferred —
     that matches the Linux ground-truth pcap.
  4. Press ENTER below.
============================================================
EOF
read -r _

echo "[*] Waiting up to 30 s for $TARGET_DESC to enumerate..."
DEV_LINE=""
for i in $(seq 1 30); do
    DEV_LINE="$(lsusb -d "$VID:$PID" | head -1)"
    [[ -n "$DEV_LINE" ]] && break
    sleep 1
done
if [[ -z "$DEV_LINE" ]]; then
    echo "[FATAL] $TARGET_DESC did not appear within 30 s. Aborting."
    exit 1
fi

# parse "Bus 004 Device 026: ID 0e8d:7961 ..."
BUS_RAW="$(echo "$DEV_LINE" | awk '{print $2}')"
BUS="$(echo "$BUS_RAW"      | sed 's/^0*//')"     # 4
ADDR="$(echo "$DEV_LINE"    | awk '{print $4}' | tr -d :)"  # 26
echo "[*] Device at bus=$BUS addr=$ADDR"
{
    echo "lsusb=$DEV_LINE"
    echo "bus=$BUS"
    echo "addr=$ADDR"
} > "$OUT_DIR/device_location_post_replug.txt"

# find this device's sysfs node
DEV_SYSFS=""
for ven in /sys/bus/usb/devices/*/idVendor; do
    [[ -f "$ven" ]] || continue
    [[ "$(cat "$ven")" == "$VID" ]] || continue
    devpath="${ven%/idVendor}"
    [[ "$(cat "$devpath/idProduct" 2>/dev/null)" == "$PID" ]] || continue
    DEV_SYSFS="$devpath"
    break
done

if [[ -n "$DEV_SYSFS" ]]; then
    DEV_PORT="$(basename "$DEV_SYSFS")"   # e.g. "4-2"
    echo "[*] sysfs=$DEV_SYSFS (port=$DEV_PORT)"
    {
        echo "sysfs=$DEV_SYSFS"
        echo "port=$DEV_PORT"
        echo
        for key in idVendor idProduct bcdUSB bcdDevice speed version \
                   manufacturer product serial bMaxPower bNumConfigurations \
                   bConfigurationValue bDeviceClass; do
            [[ -f "$DEV_SYSFS/$key" ]] && printf "%-22s = %s\n" "$key" "$(cat "$DEV_SYSFS/$key")"
        done
        echo
        echo "# interface driver bindings (post-replug, pre-test):"
        shopt -s nullglob
        for iface in /sys/bus/usb/devices/${DEV_PORT}:*; do
            [[ -d "$iface" ]] || continue
            ifnum="$(basename "$iface")"
            if [[ -L "$iface/driver" ]]; then
                drv="$(basename "$(readlink -f "$iface/driver")")"
            else
                drv="(none)"
            fi
            printf "  %s -> %s\n" "$ifnum" "$drv"
        done
        shopt -u nullglob
    } > "$OUT_DIR/device_sysfs_post_replug.txt"
else
    echo "[WARN] could not locate device in sysfs (proceeding anyway)"
fi

lsusb                                                      > "$OUT_DIR/lsusb_post_replug.txt"
lsusb -t                                                   > "$OUT_DIR/lsusb_tree_post_replug.txt"
sudo lsusb -v -d "$VID:$PID"                               > "$OUT_DIR/lsusb_verbose_post_replug.txt" 2>&1 || true
sudo cat /sys/kernel/debug/usb/devices                     > "$OUT_DIR/usb_devices_post_replug.txt" 2>/dev/null || true

# ---- start streaming captures ------------------------------------------------
# usbmon0 captures ALL USB buses. We'll filter post-hoc by (bus, dev_addr).
# This makes us robust to mid-session bus migration (which we saw last time).
echo "[*] Starting dmesg follower + usbmon0 capture..."
sudo bash -c "exec dmesg -wT > '$OUT_DIR/dmesg_during.txt' 2>&1" &
DMESG_PID=$!

sudo tshark -i usbmon0 -w "$OUT_DIR/usbmon0.pcap" -q \
    > "$OUT_DIR/tshark.log" 2>&1 &
TSHARK_PID=$!

# give tshark a beat to actually open the capture iface
sleep 2

# ---- run the test ------------------------------------------------------------
echo "[*] Running test_hw_mt7921au.py --debug ..."
TEST_START_EPOCH="$(date -u +%s.%N)"
TEST_START_WALL="$(date -u --iso-8601=ns)"
echo "test_start_epoch=$TEST_START_EPOCH"  > "$OUT_DIR/test_window.txt"
echo "test_start_wall=$TEST_START_WALL"   >> "$OUT_DIR/test_window.txt"

set +e
( cd "$REPO_ROOT" && sudo "$VENV_PY" "$TEST_SCRIPT" --debug ) \
    > "$OUT_DIR/test_stdout.txt" \
    2> "$OUT_DIR/test_stderr.txt"
TEST_RC=$?
set -e

TEST_END_EPOCH="$(date -u +%s.%N)"
TEST_END_WALL="$(date -u --iso-8601=ns)"
echo "test_end_epoch=$TEST_END_EPOCH"     >> "$OUT_DIR/test_window.txt"
echo "test_end_wall=$TEST_END_WALL"       >> "$OUT_DIR/test_window.txt"
echo "test_rc=$TEST_RC"                   >> "$OUT_DIR/test_window.txt"

echo "[*] Test exit code: $TEST_RC"
echo "[*] Settling 5 s so any delayed kernel messages land in dmesg_during..."
sleep 5

# ---- stop streaming captures ------------------------------------------------
echo "[*] Stopping tshark + dmesg follower..."
sudo kill "$TSHARK_PID" 2>/dev/null || true
# give tshark a beat to flush the pcap
sleep 1
sudo kill "$DMESG_PID"  2>/dev/null || true
wait "$TSHARK_PID" 2>/dev/null || true
wait "$DMESG_PID"  2>/dev/null || true
TSHARK_PID=""
DMESG_PID=""

# ---- post-test snapshot ------------------------------------------------------
echo "[*] Recording post-test state..."
lsusb                                                      > "$OUT_DIR/lsusb_post_test.txt"
lsusb -t                                                   > "$OUT_DIR/lsusb_tree_post_test.txt"
sudo lsusb -v -d "$VID:$PID"                               > "$OUT_DIR/lsusb_verbose_post_test.txt" 2>&1 || true
sudo cat /sys/kernel/debug/usb/devices                     > "$OUT_DIR/usb_devices_post_test.txt" 2>/dev/null || true
sudo dmesg -T                                              > "$OUT_DIR/dmesg_post.txt" 2>&1
journalctl -k --since "10 minutes ago" --no-pager          > "$OUT_DIR/journalctl_kernel_recent.txt" 2>&1 || true

# bus-migration check: where did the device end up?
DEV_LINE_POST="$(lsusb -d "$VID:$PID" | head -1)"
echo "$DEV_LINE_POST" > "$OUT_DIR/device_location_post_test.txt"
if [[ -n "$DEV_LINE_POST" ]]; then
    BUS_POST="$(echo "$DEV_LINE_POST" | awk '{print $2}' | sed 's/^0*//')"
    ADDR_POST="$(echo "$DEV_LINE_POST" | awk '{print $4}' | tr -d :)"
    {
        echo "pre_test_bus=$BUS post_test_bus=$BUS_POST"
        echo "pre_test_addr=$ADDR post_test_addr=$ADDR_POST"
        if [[ "$BUS" != "$BUS_POST" || "$ADDR" != "$ADDR_POST" ]]; then
            echo "MIGRATED=yes — device moved during test (was on bus=$BUS addr=$ADDR, now bus=$BUS_POST addr=$ADDR_POST)"
        else
            echo "MIGRATED=no"
        fi
    } > "$OUT_DIR/device_migration.txt"
else
    echo "MIGRATED=device_gone" > "$OUT_DIR/device_migration.txt"
fi

# sysfs driver bindings AFTER the test — answers "did kernel re-grab If 3?"
if [[ -n "$DEV_SYSFS" && -d "$DEV_SYSFS" ]]; then
    DEV_PORT="$(basename "$DEV_SYSFS")"
    {
        echo "# interface driver bindings (post-test):"
        shopt -s nullglob
        for iface in /sys/bus/usb/devices/${DEV_PORT}:*; do
            [[ -d "$iface" ]] || continue
            ifnum="$(basename "$iface")"
            if [[ -L "$iface/driver" ]]; then
                drv="$(basename "$(readlink -f "$iface/driver")")"
            else
                drv="(none)"
            fi
            printf "  %s -> %s\n" "$ifnum" "$drv"
        done
        shopt -u nullglob
    } > "$OUT_DIR/device_sysfs_post_test.txt"
fi

# ---- pcap quick-summary -----------------------------------------------------
echo "[*] Summarizing pcap..."
{
    echo "# total packets by bus (sanity check):"
    tshark -r "$OUT_DIR/usbmon0.pcap" -T fields -e usb.bus_id 2>/dev/null \
        | sort | uniq -c | sort -rn
    echo
    echo "# distinct (bus, addr, vid, pid) tuples seen on the wire:"
    tshark -r "$OUT_DIR/usbmon0.pcap" -T fields \
        -e usb.bus_id -e usb.device_address -e usb.idVendor -e usb.idProduct \
        2>/dev/null | sort -u | grep -v '^[[:space:]]*$'
    echo
    echo "# first 80 transfers to/from MT7921AU (bus=$BUS, addr=$ADDR):"
    tshark -r "$OUT_DIR/usbmon0.pcap" \
        -Y "usb.bus_id == $BUS && usb.device_address == $ADDR" \
        -T fields \
        -e frame.number -e frame.time_relative \
        -e usb.endpoint_address -e usb.transfer_type -e usb.urb_status \
        -e usb.data_len \
        2>/dev/null | head -80
    echo
    echo "# packet count per endpoint on MT7921AU only:"
    tshark -r "$OUT_DIR/usbmon0.pcap" \
        -Y "usb.bus_id == $BUS && usb.device_address == $ADDR" \
        -T fields -e usb.endpoint_address 2>/dev/null \
        | sort | uniq -c | sort -rn
} > "$OUT_DIR/pcap_summary.txt" 2>&1

# carve out an MT7921AU-only pcap for fast offline analysis
sudo tshark -r "$OUT_DIR/usbmon0.pcap" \
    -Y "usb.bus_id == $BUS && usb.device_address == $ADDR" \
    -w "$OUT_DIR/usbmon0_mt7921au_only.pcap" 2>>"$OUT_DIR/tshark.log" || true

# also a smaller pcap that drops the noisy bulk-IN polling, if you only want
# control transfers + bulk OUT for fault-isolation
sudo tshark -r "$OUT_DIR/usbmon0.pcap" \
    -Y "usb.bus_id == $BUS && usb.device_address == $ADDR && (usb.transfer_type == 0x02 || usb.endpoint_address.direction == 0)" \
    -w "$OUT_DIR/usbmon0_mt7921au_ctrl_and_out.pcap" 2>>"$OUT_DIR/tshark.log" || true

# ---- chown so the user can read everything ----------------------------------
if [[ -n "${SUDO_USER:-}" ]]; then
    sudo chown -R "$SUDO_USER:$SUDO_USER" "$OUT_BASE" 2>/dev/null || true
else
    sudo chown -R "$USER:$USER" "$OUT_BASE" 2>/dev/null || true
fi

# ---- tarball ----------------------------------------------------------------
BUNDLE="$OUT_BASE/wifit3-mt7921au-$TS.tar.gz"
echo "[*] Bundling -> $BUNDLE"
tar -czf "$BUNDLE" -C "$OUT_BASE" "$(basename "$OUT_DIR")"
sudo chown "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$BUNDLE" 2>/dev/null || true

# ---- final verdict ----------------------------------------------------------
echo
echo "============================================================"
echo "  RESULT"
echo "    test exit code:    $TEST_RC"
echo "    bundle:            $BUNDLE"
echo "    bundle size:       $(du -h "$BUNDLE" | cut -f1)"
echo
echo "  Last 8 lines of test output:"
tail -8 "$OUT_DIR/test_stdout.txt" | sed 's/^/    /'
echo
echo "  Bus / driver-binding state:"
cat "$OUT_DIR/device_migration.txt"        2>/dev/null | sed 's/^/    /'
echo
if [[ -f "$OUT_DIR/device_sysfs_post_test.txt" ]]; then
    cat "$OUT_DIR/device_sysfs_post_test.txt" | sed 's/^/    /'
fi
echo
echo "  Copy the bundle back to the dev box, e.g.:"
echo "    scp $BUNDLE user@windows-host:/path/"
echo "============================================================"
