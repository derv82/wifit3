#!/bin/bash

# 1. Neutralize wpasupplicant/NetworkManager for wlan1 only
echo "[+] killing interferingi processes..."
sudo airmon-ng check kill

echo "--- READY ---"
echo "1. Start Wireshark on usbmon (Bus 3)"
echo "2. Press ENTER when Wireshark is capturing..."
read

echo "[+] Step 1: Plug in the AWUS036H now."
sleep 10

echo "[+] Step 2: Enabling Monitor Mode..."
sudo airmon-ng start wlan1
sleep 5

echo "[+] Step 3: Setting Channel 6..."
sudo iw dev wlan1 set channel 6
sleep 5

echo "[+] Step 4: Setting Channel 1..."
sudo iw dev wlan1 set channel 1
sleep 5

echo "[+] Step 5: Starting Passive Scan & Injection (20 seconds)..."
sudo airodump-ng --channel 1 wlan1 & 
AIRO_PID=$!
sleep 5
sudo aireplay-ng -a aa:bb:cc:dd:ee:01 --test  wlan1
sleep 5

sudo kill $AIRO_PID
echo "[+] Step 6: Stopping Monitor Mode..."
sudo airmon-ng stop wlan1
sleep 5

echo "[+] Step 7: Unplug the AWUS036H now."
sleep 5

echo "--- FINISHED ---"
echo "Stop Wireshark and save as .pcap" 
