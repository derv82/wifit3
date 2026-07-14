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
passes a non-default value (`attacks/wps/association.py::send`, `not self.ack`). The state
belongs at the interface; the driver should obey a bool it's handed.

### `WpsAssociation` / `WlanTransport` trapped in `attacks/wps/`
They're generic send / recv / await-reply primitives, but `pmkid_harvest` (not a WPS attack)
reaches into the WPS namespace to import `WpsAssociation`. Mislocated module boundary. Keep it
as **one** class in a neutral home — do not grow a new abstraction layer around it.

### Two homes for 802.11 address-role logic
`wlan/packet.py` maps addr1/2/3 → (source, dest, bssid) by the DS bits; `interface._client_mac`
separately decides which of those is the client STA. One concept split across two files.
Consolidate in place — no new module.

### Duplicated frame construction
`auth_req` / `assoc_req` are byte-identical between `attacks/wep/fake_auth.py` and
`attacks/wps/association.py`; the deauth MPDU bytes are hand-rolled in three spots
(`wlan/interface.py`, `attacks/pmkid_harvest.py`, `attacks/wps/association.py`). Dedupe within
the existing files.

### Scattered resend / timing magic numbers
Resend intervals for "one protocol step" disagree across ~10 files (0.4 / 0.05 / 0.02 / 0.3 s)
with no shared rationale. Give them one named home with a reason per value.

### Dead driver probe in `focus_model.card_identity`
`getattr(driver, "chipset", None)` is 0/22 — always None, always falls back to the card
description. Harmless but dead; remove the probe or implement it. _(The sibling `card_mac`/`mac`
probe was the card-endpoint MAC-display bug — now fixed to read `mac_address`.)_
