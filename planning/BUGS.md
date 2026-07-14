# Wifit3 — Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution — the fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## Bugs (behavioral)

### ACK detection counts unrelated ACKs — `_our_tx_macs` never clears
Each driver keeps an `.add()`-only set of every source MAC it has injected as, checked in the
RX path to decide whether an observed ACK is "ours." It's never reset by `enable_ack_detect`,
and it absorbs **spoofed** source MACs — a deauth's Addr2 is the AP itself. So a real station's
ordinary ACK to that AP gets counted as delivery of *our* injected frame, corrupting the deauth
per-direction ACK tally. We only ever spoof one MAC at a time and ACK correlation is inherently
stop-and-wait, so a single current-MAC value (updated per send) is enough — the persistent set
is both wrong and unnecessary. _Location: each driver's `inject_frame` + RX-ACK path._

## Design debt / leaky abstractions

### `use_no_ack` lives in the wrong layer
`inject_frame(..., use_no_ack=...)` is threaded through all 22 driver signatures, but the
decision is interface-level state: are we active-monitor-armed for this TA (hardware ACKs) or
just spoofing in monitor (no hardware ACK)? The interface already tracks the forged MAC
(`forged_macs` / `set_fake_mac`); the driver re-derives a redundant copy. Only one caller ever
passes a non-default value (`attacks/auth_assoc.py::WlanTransport.send`, `not self.ack`). The state
belongs at the interface; the driver should obey a bool it's handed.

### Two homes for 802.11 address-role logic
`wlan/packet.py` maps addr1/2/3 → (source, dest, bssid) by the DS bits; `interface._client_mac`
separately decides which of those is the client STA. One concept split across two files.
Consolidate in place — no new module.

### Duplicated deauth-frame construction
The deauth MPDU bytes are hand-rolled in three spots — `wlan/interface.py::_deauth_frame`,
`attacks/pmkid_harvest.py::_build_deauth`, and `attacks/auth_assoc.py::build_client_leaving`.
Dedupe within the existing files. (`fake_auth`'s auth/assoc builders overlap
`auth_assoc.Association`'s byte-for-byte but are deliberately left separate — WEP's lazy
lifecycle differs; not a dup to chase.)
