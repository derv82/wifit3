#!/usr/bin/env bash
#
# Configure the mt76 USB stack to DISABLE scatter-gather, so usbmon can actually
# capture the device->host (RX) data — beacons, MCU responses, the firmware-up
# signal. Without this, mt76's RX URBs use scatter-gather buffers and usbmon only
# snapshots urb->transfer_buffer (NULL for SG), so every inbound transfer is
# captured as a header with zero data. See chips/mt7921au/MT7921AU.md ("blind spot").
#
# IMPORTANT: this does NOT fix the FW_START / -110 boot wedge. Confirmed on the
# sibling mt7925u (morrownr/USB-WiFi#688): disabling SG "changed nothing" on boot.
# It ONLY makes the RX visible for verify_pcap / beacon analysis.
#
# The knob lives on the SHARED mt76_usb module (mt76-usb-y := usb.o), not mt7921u,
# so it affects EVERY mt76 USB adapter until you remove the conf (--remove).
#
# Run as root on the Linux capture box, with the card UNPLUGGED:
#   sudo bash modprobe_config.sh            # write the conf
#   sudo bash modprobe_config.sh --remove   # undo it
#
set -euo pipefail

CONF=/etc/modprobe.d/wifit3-mt76-disable-sg.conf
PARAM=/sys/module/mt76_usb/parameters/disable_usb_sg

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "error: must run as root  ->  sudo bash $0" >&2
    exit 1
fi

# --- undo ---------------------------------------------------------------------
if [[ "${1:-}" == "--remove" ]]; then
    if [[ -e "$CONF" ]]; then
        rm -f "$CONF"
        echo "[ok] removed $CONF"
    else
        echo "[ok] $CONF was not present"
    fi
    echo "     reboot (or 'sudo modprobe -r mt7921u mt76_usb' with the card unplugged)"
    echo "     to put scatter-gather back."
    exit 0
fi

# --- write the conf -----------------------------------------------------------
echo "options mt76_usb disable_usb_sg=1" > "$CONF"
echo "[ok] wrote $CONF:"
sed 's/^/       /' "$CONF"
echo

# --- handle the "module already loaded" gotcha --------------------------------
# Module params are read at LOAD time. If mt76_usb is already loaded, the conf
# does nothing until it reloads.
if [[ ! -d /sys/module/mt76_usb ]]; then
    echo "[ok] mt76_usb is not loaded yet — the param will apply the moment the"
    echo "     card enumerates during your capture. Nothing else to do."
else
    cur=$(cat "$PARAM" 2>/dev/null || echo '?')
    if [[ "$cur" == "Y" ]]; then
        echo "[ok] mt76_usb already loaded WITH scatter-gather disabled (=$cur). Ready."
    else
        echo "[!]  mt76_usb is ALREADY loaded (disable_usb_sg=$cur) — the conf is in"
        echo "     place but WON'T take effect until the module reloads. With the card"
        echo "     UNPLUGGED, reload the stack (no reboot needed):"
        echo
        echo "         sudo modprobe -r mt7921u mt76_usb"
        echo
        echo "     (if mt76_usb is still loaded afterwards, it has other users —"
        echo "      unplug all mt76 adapters, or just reboot.)"
    fi
fi

# --- how to confirm it actually took ------------------------------------------
echo
echo "VERIFY after the card loads (during / right after a capture):"
echo "    cat $PARAM"
echo "    # must print 'Y'.  If it prints 'N', the module did not reload — see above."
echo
echo "Reminder: this is the SHARED mt76_usb module, so it's on for every mt76 USB"
echo "card until you undo it:  sudo bash $0 --remove"
