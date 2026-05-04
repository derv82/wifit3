import threading
import time
from typing import Dict, Callable, Optional
from scapy.all import sniff, Dot11Beacon, Dot11ProbeResp, Dot11, RadioTap, Dot11Elt
from loguru import logger

from .models import AccessPoint

class Scanner:
    def __init__(self, callback: Callable[[AccessPoint], None]):
        self.access_points: Dict[str, AccessPoint] = {}
        self.callback = callback
        self.is_running = False
        self._thread: Optional[threading.Thread] = None

    def _handle_packet(self, packet):
        # DIAGNOSTIC: Log every packet summary
        logger.debug(f"PKT: {packet.summary()}")

        if not (packet.haslayer(Dot11Beacon) or packet.haslayer(Dot11ProbeResp)):
            return

        bssid = packet[Dot11].addr3
        logger.debug(f"Found AP: {bssid}")
        
        # Extract SSID from Dot11Elt (Information Element)
        if packet.haslayer(Dot11Elt):
            elt = packet[Dot11Elt]
            while elt:
                if elt.ID == 0:  # ID 0 is SSID
                    try:
                        ssid = elt.info.decode(errors="ignore")
                    except Exception:
                        ssid = "<Hidden>"
                    break
                elt = elt.payload.getlayer(Dot11Elt)

        if not ssid:
            ssid = "<Hidden>"

        # Extract Signal Strength (RSSI) from RadioTap
        signal = -100
        if packet.haslayer(RadioTap):
            # Try a few common field names Scapy uses for signal strength
            for field in ["dBm_AntSignal", "dB_AntSignal", "dBm_signal"]:
                val = getattr(packet, field, None)
                if val is not None:
                    try:
                        signal = int(val)
                        break
                    except (TypeError, ValueError):
                        continue

        if bssid not in self.access_points:
            ap = AccessPoint(bssid=bssid, ssid=ssid, signal=signal)
            self.access_points[bssid] = ap
        else:
            ap = self.access_points[bssid]
            ap.signal = signal
            ap.beacons += 1
            if ssid and (not ap.ssid or ap.ssid == "<Hidden>"):
                ap.ssid = ssid
        
        # Trigger the TUI update
        self.callback(ap)

    def start(self, interface: Optional[str] = None):
        if self.is_running:
            return
        
        self.is_running = True
        self._thread = threading.Thread(target=self._run_sniff, args=(interface,), daemon=True)
        self._thread.start()
        logger.info(f"Scanner started on interface: {interface or 'default'}")

    def _run_sniff(self, interface: Optional[str]):
        # Give the driver a moment to stabilize after mode switch
        time.sleep(2.0)
        try:
            logger.debug(f"Starting sniff on {interface} with monitor=True...")
            sniff(
                iface=interface, 
                prn=self._handle_packet, 
                store=0, 
                monitor=True,
                stop_filter=lambda x: not self.is_running
            )
        except Exception as e:
            logger.error(f"Scanner error: {e}")
            self.is_running = False

    def stop(self):
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        logger.info("Scanner stopped")

    def simulate_discovery(self):
        """Mock method for testing the UI without hardware."""
        import random
        mock_bssids = ["00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF", "DE:AD:BE:EF:CA:FE"]
        mock_ssids = ["Home_WiFi", "Starbucks", "Free_Public_WiFi"]
        
        while self.is_running:
            idx = random.randint(0, 2)
            ap = AccessPoint(
                bssid=mock_bssids[idx],
                ssid=mock_ssids[idx],
                signal=random.randint(-90, -30),
                beacons=random.randint(1, 100)
            )
            self.callback(ap)
            time.sleep(1)
