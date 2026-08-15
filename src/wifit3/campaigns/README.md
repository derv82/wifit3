# campaigns/

Attack implementations. Each takes a `WlanInterface` and a target from `models/`
(`AccessPoint` / `Client`) and runs natively on the interface primitives (no
`aircrack-ng` / `reaver` / `hcxdumptool` subprocess).

What lives here:
- `campaign.py`: the radio-owning `Campaign` base every attack subclasses.
- `auth_assoc.py`: the forged-client auth/assoc machinery the WEP and WPS attacks reuse.
- `pmkid.py`: PMKID capture (passive and active).
- `eviltwin/`: WPA2 twin that punts clients off a WPA3-transition AP to capture a crackable 4-way.
- `decloak.py`: hidden-SSID reveal.
- `pin.py` / `pbc.py`: WPS PIN brute-force (Registrar) and PBC capture (Enrollee).
- `wep/`: the WEP campaign (fake-auth, ARP replay, ChopChop). See [`wep/README.md`](wep/README.md).
- `wps/`: the WSC machinery the PIN/PBC campaigns drive. See [`wps/README.md`](wps/README.md).

The attacks build on `WlanInterface` primitives: `send_no_wait` / `send_until_ack`
inject a raw 802.11 frame, `deauth_broadcast` / `deauth_client` send a no-ACK deauth
burst, and `register_rx_callback(cb)` gives a low-latency RX feed that bypasses the
UI-polled AP registry (how the WEP/WPS state machines run).

Active TX is passive by default, behind explicit Focus actions.
