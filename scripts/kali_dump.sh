#!/usr/bin/env bash
#
# Wifit3 Kali Info Dump — fully self-contained, fast, no pcap noise.
# Just copy this single file to Kali. No repo needed.
#
#   sudo bash kali_dump.sh
#
# Gathers: kernel version, mt76/mt7921 driver versions, firmware blobs (with
# SHA256), USB descriptors and dmesg for the device on USB 3.0 and USB 2.0.
# When done, ~/kalitrip.tar.gz is what you bring back.

set -uo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run me with sudo (need root for dmesg / firmware files)."
    exit 1
fi

TARGET_USER="${SUDO_USER:-root}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

OUTDIR="$TARGET_HOME/kalitrip"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
echo "[*] Output directory: $OUTDIR"

# ---------------------------------------------------------------------
# 1. Static system + driver + firmware info
# ---------------------------------------------------------------------

echo ""
echo "=== Kernel ==="
uname -a | tee "$OUTDIR/uname.txt"
cat /etc/os-release > "$OUTDIR/os-release.txt" 2>/dev/null
echo "(saved $OUTDIR/uname.txt, os-release.txt)"

echo ""
echo "=== Loaded mt76* / mt7921* modules ==="
lsmod | grep -E '^(mt76|mt7921)' | tee "$OUTDIR/lsmod.txt"

echo ""
echo "=== Driver modinfo ==="
{
    for mod in mt7921u mt7921e mt7921_common mt76_usb mt76_connac_lib mt76; do
        echo "--- $mod ---"
        modinfo "$mod" 2>&1 || echo "(not loaded / not found)"
        echo ""
    done
} > "$OUTDIR/modinfo.txt"
grep -E '^(filename|version|srcversion):' "$OUTDIR/modinfo.txt" | head -20
echo "(full: $OUTDIR/modinfo.txt)"

echo ""
echo "=== Firmware files ==="
mkdir -p "$OUTDIR/firmware"
ls -la /lib/firmware/mediatek/ > "$OUTDIR/firmware/listing.txt" 2>&1 || true
for f in /lib/firmware/mediatek/WIFI_MT7961_* /lib/firmware/mediatek/WIFI_RAM_CODE_MT7961_*; do
    [ -f "$f" ] && cp -av "$f" "$OUTDIR/firmware/"
done
sha256sum "$OUTDIR/firmware/"* > "$OUTDIR/firmware/sha256.txt" 2>&1
echo "(saved $OUTDIR/firmware/)"

# ---------------------------------------------------------------------
# 2. Per-port device info: USB 3.0 then USB 2.0
# ---------------------------------------------------------------------

dump_device_info() {
    local label="$1"
    local dir="$OUTDIR/$label"
    mkdir -p "$dir"

    sleep 1
    dmesg --ctime | tail -200 > "$dir/dmesg.txt"
    lsusb -d 0e8d:7961 > "$dir/lsusb.txt" 2>&1
    lsusb -v -d 0e8d:7961 > "$dir/lsusb_verbose.txt" 2>&1

    for ven in /sys/bus/usb/devices/*/idVendor; do
        if [ -f "$ven" ] && [ "$(cat $ven 2>/dev/null)" = "0e8d" ]; then
            local devpath="${ven%/idVendor}"
            local pid="$(cat $devpath/idProduct 2>/dev/null)"
            [ "$pid" = "7961" ] || continue
            {
                echo "device path: $devpath"
                for key in idVendor idProduct bcdUSB bcdDevice speed version manufacturer product serial bMaxPower; do
                    [ -f "$devpath/$key" ] && echo "$key = $(cat $devpath/$key 2>/dev/null)"
                done
            } > "$dir/sysfs.txt"
        fi
    done

    if [ -s "$dir/sysfs.txt" ]; then
        echo "  Speed:  $(grep '^speed'  $dir/sysfs.txt | awk -F= '{print $2}' | tr -d ' ')"
        echo "  bcdUSB: $(grep '^bcdUSB' $dir/sysfs.txt | awk -F= '{print $2}' | tr -d ' ')"
    else
        echo "  [!] Device not detected — check it's plugged in."
    fi
}

echo ""
echo "================================================================"
echo "STEP 1: USB 3.0 (no adapter). Plug into a USB 3.0 port."
echo "================================================================"
read -p "Press ENTER once plugged in (give it ~3s to enumerate)... "
dump_device_info usb3

echo ""
echo "================================================================"
echo "STEP 2: Unplug. Then plug into your USB 2.0 hub/adapter."
echo "        (the one that previously showed 'HIGH (480 Mbps)')"
echo "================================================================"
read -p "Press ENTER once plugged in on USB 2.0... "
dump_device_info usb2

# ---------------------------------------------------------------------
# 3. Archive
# ---------------------------------------------------------------------

chown -R "$TARGET_USER:$TARGET_USER" "$OUTDIR"

TARFILE="$TARGET_HOME/kalitrip.tar.gz"
tar -czf "$TARFILE" -C "$TARGET_HOME" kalitrip
chown "$TARGET_USER:$TARGET_USER" "$TARFILE"
SIZE=$(du -h "$TARFILE" | cut -f1)

echo ""
echo "============================================================"
echo "[+] Done. Archive: $TARFILE  ($SIZE)"
echo "[+] Extract into <project>/data_dumps/kalitrip/ and tell"
echo "    Claude where it is."
echo "============================================================"
