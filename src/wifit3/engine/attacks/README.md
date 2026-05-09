## `engine/attacks/*.py`

This is where the complex workflows should live (e.g., WPA2DeauthAttack, PixieDust).

The attacks take a WlanInterface and a models.Target object as arguments.

The attacks use common WlanInterface methods when applicable:
  - iface.deauth(access_point_bssid, client_bssid) -> sending a single deauth packet with ack.
  - iface.register_listener(dot11_callback, filter=None) -> for capturing & filtering 802.11 packets
  - iface.send_raw() to inject the unique specific malicious frames (Pixie/WPS, WPA3 PMKID stuff?).

Note: Skip WEP for now; low ROI for effort (WEP=DEAD).
