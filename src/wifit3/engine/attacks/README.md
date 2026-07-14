# engine/attacks/

Attack implementations. Each takes a `WlanInterface` and a target from
`engine/models.py` (`AccessPoint`/`Client`) and runs natively on the interface's
primitives — no `aircrack-ng`/`reaver`/`hcxdumptool` subprocesses.

What lives here:
- `pmkid_harvest.py` — PMKID capture (passive + active); also the forge-client +
  auth/assoc machinery the WEP/WPS attacks reuse.
- `wpa3_downgrade.py` — WPA3-transition downgrade.
- `decloak.py` — hidden-SSID reveal.
- `wep/` — fake-auth, ARP replay, ChopChop, PTW crack. → [`wep/README.md`](wep/README.md)
- `wps/` — PIN brute (Registrar) + PBC capture (Enrollee) + lock detect. → [`wps/README.md`](wps/README.md)

`WlanInterface` primitives they build on:
- `await iface.deauth(ap_bssid, client_bssid, burst_count=10)` — a no-ACK deauth burst.
- `await iface.send_no_wait(...)` / `send_until_ack(...)` — inject a raw 802.11 frame.
- `iface.register_rx_callback(cb)` — `cb(frame_bytes, rssi, timestamp)`, a low-latency RX
  feed that bypasses the UI-polled AP registry (how the WEP/WPS state machines run).

Active TX is **passive by default** — gated behind explicit Focus actions.
