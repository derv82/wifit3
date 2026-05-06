#!/bin/bash

# Default to wlan1 if no argument is passed (Usage: ./capture.sh wlan1)
BASE_IFACE=${1:-wlan1}
MON_IFACE=""

# ==============================================================================
# SAFETY NET: This trap guarantees the virtual interface is destroyed,
# preventing kernel panics if you press Ctrl+C or the script crashes.
# ==============================================================================
cleanup() {
    echo -e "\n[!] Teardown initiated. Cleaning up virtual interfaces..."
    if [ -n "$MON_IFACE" ]; then
        sudo airmon-ng stop $MON_IFACE >/dev/null 2>&1
    else
        # Fallbacks just in case it crashed early
        sudo airmon-ng stop ${BASE_IFACE}mon >/dev/null 2>&1
        sudo airmon-ng stop $BASE_IFACE >/dev/null 2>&1
    fi
    echo "[+] Interfaces cleanly destroyed. It is now safe to unplug."
    exit 0
}
trap cleanup SIGINT SIGTERM ERR

# ==============================================================================

echo "[+] Killing interfering processes..."
sudo airmon-ng check kill >/dev/null

echo "--- READY ---"
echo "0. Run 'sudo modprobe usbmon' in another terminal"
echo "1. Start Wireshark on the correct usbmon (Check lsusb for Bus #)"
echo "2. Press ENTER when Wireshark is capturing..."
read

echo "[+] Step 1: Plug in the Ralink AWUS051NH now."
echo "[*] Waiting 10 seconds for USB enumeration..."
sleep 10

echo "[+] Step 2: Enabling Monitor Mode on $BASE_IFACE..."
sudo airmon-ng start $BASE_IFACE >/dev/null
sleep 3

# Dynamically detect whatever name mac80211 assigned to the monitor interface
MON_IFACE=$(iw dev | awk '$1=="Interface"{iface=$2} $1=="type" && $2=="monitor"{print iface}' | head -n 1)

if [ -z "$MON_IFACE" ]; then
    echo "[-] Failed to detect monitor interface. Did the card enumerate as $BASE_IFACE?"
    cleanup
fi

echo "[+] Success! Monitor interface detected as: $MON_IFACE"

echo "[+] Step 3: Setting Channel 6..."
sudo iw dev $MON_IFACE set channel 6
sleep 5

echo "[+] Step 4: Setting Channel 1..."
sudo iw dev $MON_IFACE set channel 1
sleep 5

echo "[+] Step 5: Starting Passive Scan & Injection (20 seconds)..."
sudo airodump-ng --channel 1 $MON_IFACE & 
AIRO_PID=$!
sleep 5

echo "[+] Step 6: Firing Injection Test..."
sudo aireplay-ng -a aa:bb:cc:dd:ee:01 --test $MON_IFACE
sleep 5

sudo kill $AIRO_PID 2>/dev/null

# The trap will automatically handle the teardown from here
cleanup
