# WLAN stack simplification

Scope: make the `WlanInterface` / `WlanArray` / `WlanSink` stack a clean, direct API so
campaigns are small state machines that never wire MAC/channel/ACK plumbing themselves.

Status: migrated to the lease API and committed: PMKID, WPS PIN, WPS PBC, WEP. New
`Deauth` campaign added.
Remaining: EvilTwin (a decision, section 2), the shared cleanup (section 3).

---

## 1. The contract (how a campaign touches the stack)

**Config + lifecycle: `array.lease(...)`** (`src/wifit3/wlan/lease.py`) is the only way a
campaign configures an interface. It arms channel + fake-MAC (active monitor) + own-MAC
registration + ACK tally on enter, and restores each on exit.

```python
# lexical (per-attempt): PMKID, WPS PBC
async with self.array.lease(channel=ch, fake_mac=mac, bssid=b, ack_tally=True) as iface:
    if lease.mac: self.our_mac = str_to_mac(lease.mac)
    await iface.send_no_wait(frame)
    resp = await self.array.next_frame(is_authresp_to_us, timeout=1.0)

# imperative (campaign-lifetime): WPS PIN, WEP. store the lease, acquire/release.
self._lease = self.array.lease(channel=ch, fake_mac=mac, bssid=b, ack_tally=True)
await self._lease.acquire()
await self._lease.rearm(new_mac)   # swap the fake MAC mid-lease (WPS re-auth)
await self._lease.release()        # in teardown()
```

- `fake_mac`: `SPOOFABLE` (random LAA), a concrete MAC (bytes/str), or `None` (no arm).
- `lease.mac` = the MAC armed and registered as own. With a concrete MAC it registers even
  when the card can't active-monitor, so the own-TX drop still holds (WEP relies on this).
- **RX: read the sink**, not a per-interface callback. `array.next_frame(match, timeout)`
  returns the first deduped RX frame any card for which `match(pkt)` is true; `wait_until`
  polls sink state (Deauth uses it for its capture stop condition).
- **TX stays per-interface**: `iface.send_no_wait` / `iface.send_until_ack`.
- **Own-frame identity is `sink.own_macs`**: a frame whose TA (Addr2) is in it is dropped
  from `packet_stats` and AP/client state, and our STA is never tracked as a client. Thin
  `register_forged_mac` / `register_self_mac` shims remain for the not-yet-migrated callers
  (Decloak, EvilTwin).

---

## 2. EvilTwin: keep same-BSSID, or not?

EvilTwin works today, both same-BSSID (on a decoy channel) and distinct-BSSID (same
channel). The same-BSSID path leans on ad-hoc filters, and cleaning them up runs straight
into the `AccessPoint` keying. So this is a decision before any code, not a migration.

**Why same-BSSID matters:** it is the only attack that extracts M1+M2 from a modern iOS
client without the user typing the PSK. Distinct-BSSID relies on the client voluntarily
roaming to an off-by-one-BSSID twin, which iOS resists. Dropping same-BSSID loses that.

**Crypto + capture files (settled):** the captured M2's MIC binds AA = the authenticator
MAC = the twin's BSSID (the client is talking to the twin). The recovered credential is
still the real network's PSK (PMK = PBKDF2(PSK, SSID), no BSSID). So a capture is correctly
stored under the twin's BSSID and cracks with the twin BSSID as `MAC_AP`; no re-attribution
into the capture file. Add a `_twin` suffix to the filename (`_{ssid}_twin.{hc22000,pcap}`)
so it reads clearly; the ESSID in the file already names the network.

### Direction A: distinct-BSSID only (drop same-BSSID)
The twin gets its own BSSID and its own `AccessPoint` record. No beacon bleed, no shared
handshake slot, no channel scoping. The whole cleanup collapses to one concept, "this BSSID
is our own AP": hide it from the Scanner target list, keep its record for our capture.
`ignore_stray_beacons` disappears entirely. Cost: lose the iOS feature.

### Direction B: keep same-BSSID (decoy channel)
The twin shares the real AP's BSSID but beacons on a different channel. In the current
bssid-keyed `AccessPoint` model this collides on one record. Problems it creates:

- **Beacon bleed:** the twin's WPA2 beacon overwrites the real AP's encryption/channel
  (the WPA3->WPA2 flip-flop). Today: `ignore_stray_beacons(bssid, channel)` drops the twin's
  beacons off the record.
- **AKM/M1/M2 mis-attribution:** the twin's forged M1 (our ANonce) and any real 4-way M1
  (the real AP's ANonce) land in the same `ap.handshakes[client_mac]`. Latent today: it only
  bites when both exist for one client.
- **Packet-stat bleed:** the twin's frames count toward the real AP's stats.

The clean fix for all three is to key `AccessPoint` records / handshakes / stats by
`(bssid, channel)` instead of `bssid`. **That is a massive change.** `AccessPoint` is the
god object of the project: handshakes, pmkids, stats, siblings, and every Scanner/Focus row
hang off a bssid-keyed dict, so channel-keying touches UI rendering, packet stats, handshake
capture, dedup, and the Scanner. It also has to keep a legitimate AP that appears on more
than one channel (DFS / band, or a reused BSSID) from fragmenting into two rows. Too big for
one session, and not obviously worth it. Stamping the channel only for known-twin BSSIDs is
the same spaghetti in a different sauce.

**The decision:** take Direction A (accept the iOS loss for a large simplification), or keep
Direction B as-is (the current filters work; do not attempt the channel-aware rewrite now).
Folding the drop filters into a lease-owned mechanism is only clean under channel-aware
keying, so it is parked with Direction B.

### Cleanups worth doing under either direction (non-channel)
- The twin should NOT register its BSSID in `own_macs`: `_ingest` drops on TA before
  `sink.update`, so registering it would drop the twin's own beacons, and those beacons are
  load-bearing (they create the record the captured M1/M2 attach to). Same-BSSID would also
  drop the real AP. (The old doc's "a lease registers it for free" was wrong.)
- "Hide the twin from the Scanner" + "force our M1 into handshake state" want to be one named
  concept ("our own AP"), not `mark_evil_twin` + `record_injected_eapol`.
- `fake_ap.on_rx` (probe/auth/assoc/M2) should read the sink via `next_frame`, not a raw
  per-card callback. The punter is a channel-only lease (same shape as `Deauth`).

---

## 3. Shared cleanup still open (design-first)

Design decisions, not mechanical tasks: agree module/class/method/variable naming with the
Senior Lead before writing code.

- **TX-stats path** (the worst offender, independent of EvilTwin). `iface.on_tx` ->
  `WlanSink.record_tx` re-parses a frame we built, flattens every TX subtype to one `inject`
  class, keys only by bssid, and carries no interface or producer role. Desired: a
  producer/role tag at the TX call site and `PacketStats` keyed by `(bssid, class, iface,
  role)`, with `snapshot(bssid)` summing the extra dims so dashboards stay byte-identical.
  Unlocks the later packet-dashboard redesign.
- **Per-interface RX callbacks -> `next_frame`** for cross-card RX: `Association._rx_cb` /
  `WlanTransport._rx_cb` (`auth_assoc.py`), the WEP `arp_replay`/`chopchop`/`fragmentation`/
  `fake_auth` `_rx_cb`, and `fake_ap.on_rx`. Do NOT force everything through a shared
  `Association` base; a campaign rolling its own ~6-line auth/assoc on the new API is fine.
- **`card_id` on `Packet`** (first card to see a frame stamps it; not part of the dedup key):
  only needed if Direction B goes channel-aware. Otherwise not yet.
- **AccessPoint stays a light read-model** Textual watches; heavy attack buffers live in
  bssid-keyed side-stores in the sink (as `WepCaptureStore` does).
