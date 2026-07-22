# Multicard Plan

## Context

Wifit3 drives one USB card at a time: `WifiteApp.active_interface` is a single `WlanInterface`
that both talks to the radio and owns the whole 802.11 picture (APs, clients, WEP capture,
packet stats). We want to run several cards at once so RX coverage improves (a beacon or an EAPOL
frame heard by any card lands in the shared picture) while attacks still run on one well-chosen
radio. `planning/MULTICARD-LLD.md` sketched this, but left TX handling, the RX call chain, and the
migration undecided, and it filled in some details (a session-fixed `_tx`, array-level deauth) that
we have since rejected. This plan records the decisions we agreed and the exact steps to build it.

## Decisions locked (these supersede parts of the LLD)

1. **Three layers.** `WlanInterface` = pure radio (per card). `WlanSink` = the 802.11 picture
   (one per session). `WlanArray` = pool + card selector + merged-read facade (one per session).
2. **No session-fixed `_tx`, no `use_for_tx`.** Cards are chosen on demand by
   `WlanArray.select_iface(channel, capabilities)`: filter to cards that support the channel, then
   pick the most attack-capable (`FAKE_MAC` order SPOOFABLE < FIXED_MAC < NONE < UNIMPLEMENTED).
   Stateless: no lease, callers may resolve to the same card (matches single-card behavior today).
3. **Deauth stays on `WlanInterface`** (the LLD moved it to the array; that was wrong). Focus does
   `iface = array.select_iface(...)` then `iface.deauth_broadcast/deauth_client(...)`.
4. **Campaigns take `(array, ap)`, not an iface.** Each campaign calls `select_iface` for its radio,
   reads the picture via `array` / the passed `ap`, and registers forged MACs via `array`.
5. **RX chain.** `driver._dispatch -> iface._on_frame_parsed` (fan-out only) `-> array._ingest(card_id, pkt)`
   `-> StreamMerger` dedupe `-> WlanSink.update(pkt, card_id)`. `card_id = iface.name` for v1.
   Deduped push hook (`array.register_rx_callback`) is kept with no v1 consumer. ACK frames never
   reach this chain (driver eats `0xD4` in `_dispatch` via `record_ack`, read via `acks_seen`).
6. **Channel policy.** Scanner = SPREAD (partition channels across cards). Focus = STACK
   (`array.set_channel` fans to channel-capable members). PBC (always-on, scanner-triggered) =
   stop-the-world: `array.stop_hopping()`, STACK all channel-capable cards onto the PBC channel,
   `select_iface` the injector, run M1..M8, then `array.start_hopping()`.
7. **Device loss v1:** assume no loss mid-campaign. Interactive `WlanInterface` methods raise
   `OperationOnLostDeviceError` once `_device_lost` is latched; `Campaign` (ABC) catches it and ends
   gracefully. Array `hot_unplug`: 0 cards left -> splash, 1+ -> toast and keep running.
8. **Naming:** picture class `WlanSink` in `wlan/sink.py` (the LLD called it `WlanDatastore`).
   `WlanArray` lives in `wlan/array.py`. `record_signal` (the LLD called it `note_signal`).
9. **`WepCaptureStore` re-homes from the interface to `WlanSink`** (internals untouched, keyed by
   BSSID). WEP campaign reads `array.wep_store`.
10. **TX packet-stats:** `WlanInterface` gains a thin `on_tx` event hook (same shape as
    `register_disconnect_callback`); the array points it at `WlanSink.record_tx` at pool time. The
    iface owns no stats.

## New files

- `src/wifit3/wlan/dedupe.py` — `StreamMerger`, generalized from `scripts/cross_streams.py:39-101`
  from fixed 2 int sources to dynamic N string sources (dict-keyed `rx/first/only`, register /
  unregister a source on hotplug / unplug). `key(raw) = raw[0:2] + raw[4:24]`, `window = 0.3`.
  `scripts/cross_streams.py` keeps its own copy (do not touch it).
- `src/wifit3/wlan/sink.py` — `WlanSink`: the picture + the four handlers, moved verbatim.
- `src/wifit3/wlan/array.py` — `WlanArray`: pool, selector, channel policy, merged reads.
- `tests/wlan/test_sink.py`, `tests/wlan/test_dedupe.py`, `tests/wlan/test_array.py`.

## WlanSink (the picture)

Move out of `WlanInterface` verbatim:
- fields `access_points` (`interface.py:92`), `clients` (`:93`), `wep_store` (`:95`),
  `packet_stats` (`:96`), `forged_macs` (`:98`), `self_macs` (`:99`).
- handlers `_on_beacon_frame` (`:138`), `_on_wepdata_frame` (`:243`), `_track_client` (`:255`),
  `_on_eapol_frame` (`:293`), `_recompute_siblings_for` (`:365`), `_decloak` (`:352`),
  and the module helpers `_enc_rank` / `_bssid_bit_diff` / `_bssid_byte_diff`.
- `get_access_points` (`:392`), `register_forged_mac` (`:408`), `register_self_mac` (`:459`),
  `unregister_self_mac` (`:477`), `record_tx` (ex `_record_tx`, `:551`).

New surface:
- `update(pkt, card_id)` — ex `_on_frame_parsed` body (`:113-136`): junk-BSSID filter,
  `packet_stats.record_rx`, then the four handlers. Writes `signal_by_card[card_id]` smoothed.
- `record_signal(card_id, bssid, rssi)` — per-card RSSI on a duplicate; no-op if BSSID unknown.

Zero hardware / event-loop dependencies. `signal` smoothing (`(old+rssi)//2`) moves into
`update` / `record_signal`.

### Model change

`AccessPoint` (`models/access_point.py:32`) and `Client` (`models/client.py:11`):
```python
signal_by_card: dict[str, int] = field(default_factory=dict)
@property
def signal(self) -> int:
    return max(self.signal_by_card.values(), default=-100)
```
Blast radius (assignments become `signal_by_card[card_id] = ...` inside `WlanSink`; the four write
sites at `interface.py:167,198,271,274` go away): read sites `scanner.py:371`,
`focus_model.py:312,453`, `scripts/diag/probes/baseline.py:82` are unaffected (they read `.signal`).
Tests that pin the old scalar move (see Test migration).

## WlanInterface (pure radio)

Keep: `driver, name, description, vid, pid, dev, current_channel`, `_rx_callbacks`,
`_disconnect_callbacks`, `_device_lost`, hop state (`_hopping_task, _tune_task, _is_hopping,
_hop_lock`). Methods: `connect`, `close`, `set_channel`, `start_hopping`, `stop_hopping`,
`supported_channels`, `inject_frame`/`send_no_wait`, `send_until_ack`, `deauth_broadcast`,
`deauth_client`, `set_fake_mac`, `clear_fake_mac`, `active_monitor_warning`, `enable_rx_acks`,
`disable_rx_acks`, `acks_seen`, `register_rx_callback` (raw), `register_disconnect_callback`.

Changes:
- `_on_frame_parsed(pkt)` shrinks to `for cb in self._rx_callbacks: cb(pkt)` (raw fan-out only).
- Remove all picture fields/handlers/getters (they moved to `WlanSink`).
- `set_fake_mac` no longer registers a forged MAC (that side effect moves to the caller via
  `array.register_forged_mac`); it only enters active monitor.
- Add `on_tx` hook; `send_no_wait`/`send_until_ack` fire it with the frame (array wires it to
  `WlanSink.record_tx`).
- Interactive methods (`set_channel`, `send_no_wait`, `send_until_ack`, `set_fake_mac`,
  `enable_rx_acks`, `deauth_*`, ...) raise `OperationOnLostDeviceError` when `self._device_lost`.
  Define the exception in `wlan/interface.py` (or an existing errors module).

## WlanArray (pool + selector + facade)

```
fields:  _members: list[WlanInterface]
         _sink: WlanSink
         _dedupe: StreamMerger
         _rx_callbacks, _disconnect_callbacks
         _hop_task, _mode                          # channel policy state
methods:
  async add(handle) -> WlanInterface | Exception   # build driver+iface, connect (SEQUENTIAL),
                                                    #   pool, register source in dedupe,
                                                    #   wire on_tx -> _sink.record_tx,
                                                    #   subscribe _ingest, wire disconnect
  async hotplug(handle) / async hot_unplug(iface)  # per-card add/remove; re-emit disconnect w/ len
  select_iface(channel, capabilities) -> WlanInterface | None   # stateless election (decision 2)
  register_rx_callback / register_disconnect_callback           # deduped stream
  register_forged_mac / register_self_mac / unregister_self_mac / record_tx  # -> _sink
  get_access_points / access_points / clients / forged_macs / wep_store / packet_stats  # -> _sink
  async set_channel(channel)            # STACK: fan to channel-capable members
  async start_hopping(channels) / stop_hopping()   # SPREAD: partition, iface[i].start_hopping(subset)
  _ingest(card_id, pkt)                 # dedupe -> _sink.update or _sink.record_signal
```
`_ingest` (single-threaded on the loop):
```python
def _ingest(self, card_id, pkt):
    if pkt.source in self._sink.forged_macs:
        return
    if self._dedupe.submit(card_id, pkt.raw, monotonic_now):
        self._sink.update(pkt, card_id)
        for cb in self._rx_callbacks: cb(pkt)      # deduped push, no v1 consumer
    else:
        self._sink.record_signal(card_id, pkt.bssid, pkt.rssi)
```
`add` wiring: `iface.register_rx_callback(lambda pkt, cid=iface.name: self._ingest(cid, pkt))` and
`iface.register_disconnect_callback(lambda e, cid=iface.name: self._member_lost(cid, e))`.

Single card = an array of one: dedupe is a no-op, all reads pass through, one code path.

## Campaign migration (uniform, ~8 campaigns)

Constructor `(iface, ap)` -> `(array, ap)` (also fix the base `Campaign.__init__` arg order, which
is currently `(ap, iface)` while concrete classes pass `(iface, ap)`: standardize on `(array, ap)`).
Inside `run()`:
```
iface = self.array.select_iface(self.ap.channel, needs_spoof=<True for active-monitor attacks>)
if iface is None: <log "no card can reach this AP's band", end>
```
Then `s/self.iface/iface/` for radio ops; `self.iface.access_points.get(bssid)` -> `self.ap`;
`self.iface.register_forged_mac(m)` -> `self.array.register_forged_mac(m)`;
`self.iface.register_self_mac/unregister_self_mac` -> `self.array....`. Wrap the run body so
`OperationOnLostDeviceError` ends it gracefully (do this once in the ABC).

Call sites: `pmkid.py` (121,130,142,183,188,251), `wpa3_downgrade.py` (91,104,151),
`decloak.py` (78,89,90,98,109,116), `auth_assoc.py` (74,75,81,86,91,137,143,147,148,176),
`wep/*` (fake_auth 110,111,120,121,200,206,208; arp_replay 149,157,308; chopchop 181,190,291,518),
`wps/pin.py` (203,205,240,389,432,484), `wps/pbc.py` (66,69,75,101,105,106),
`wps/registrar.py`, `wps/enrollee.py` (the `send_until_ack`/`send_no_wait`/`recv` Transport is
unchanged; it just runs on the selected iface).

## UI migration

- `app.py`: `active_interface` -> `array: WlanArray` (`:114`). `notify_device_lost` /
  `recover_to_splash` (`:129,146`) become the array `hot_unplug` policy (0 -> splash, 1+ -> toast).
  `action_quit` closes the array.
- `splash.py`: discovery now lists device handles (see Manager). On select, `await array.add(handle)`;
  handoff at `:523-525` sets `app.array` and subscribes the array disconnect policy.
  Hotplug modal (LLD copy) calls `array.hotplug(handle)`.
- `scanner.py`: `self.app.active_interface` -> `self.app.array`. `get_access_points` (`:274,605,636`),
  `start_hopping`/`stop_hopping` (`:225,668,697,766,768,814`), `set_channel` (`:669`),
  `supported_channels` (`:179,740`), `clients`/`forged_macs` (`:260,261,325`),
  `access_points` iterate/pop (`:346,419,792,796`) all read the array. `_invade_pbc` (`:659`)
  uses the stop-the-world PBC flow.
- `focus_v2/screen.py`: `getattr(self.app,"active_interface",None)` -> `self.app.array`. Deauth
  (`:622,639`): `array.select_iface` then `iface.deauth_*`. Campaign construction
  (`:676,734,782,833,882`) passes `(array, ap)`. `set_channel`/STACK (`:225,325`),
  `forged_macs` (`:435`), `packet_stats` (`:468`) read the array.
- `focus_model.py`, `packet_dashboard.py`: `iface.<picture>` -> `array.<picture>`;
  `card_identity` (`:422-428`) reports the selected/first member (or "N cards").

## WlanDeviceManager (discovery only)

Today `refresh()` (`manager.py:152`) closes and rebuilds every interface on any bus change
(`:158-165`), which would kill live pooled cards. Change to discovery-only:
- `refresh()` returns device handles `(dev, driver_cls, id_entry)` from `_scan_bus`; it does NOT
  build or close `WlanInterface`s.
- Keep `_dev_sig` diffing but expose added/removed handles (`added_since()` / `removed_since()`),
  so the session loop can drive `array.hotplug(handle)` / `array.hot_unplug(iface)` per card.
- `is_openable`, the Linux permission/kernel-driver helpers, and `_match_driver` are unchanged.
- Interface construction (`from_usb_device` + `WlanInterface(...)`) moves into `WlanArray.add`.

## Build sequence (each step keeps the tree green)

1. `dedupe.py` + `test_dedupe.py` (pure, no deps). Generalize to N string sources.
2. Model change: `signal_by_card` + `signal` property on `AccessPoint`/`Client`; update the two
   model tests.
3. `sink.py` + `test_sink.py`: move the picture + handlers + `update`/`record_signal`
   (drive it with `Packet` inputs; this replaces the interface picture tests).
4. Slim `WlanInterface`: remove picture, shrink `_on_frame_parsed`, add `on_tx` hook +
   `OperationOnLostDeviceError`. Rewire `set_fake_mac`. Keep `scripts/ack_lab/*` and
   `scripts/cross_streams.py` working (they only use `.driver`, raw `register_rx_callback`,
   `connect/set_channel/close`, `.description`, `SUPPORTED_CHANNELS`).
5. `array.py` + `test_array.py`: pool, `_ingest`, `select_iface`, channel policy,
   merged reads, `add/hotplug/hot_unplug`. Array-of-one regression test.
6. Campaign ABC: `(array, ap)` + `OperationOnLostDeviceError` handling. Then migrate the 8 campaigns.
7. Manager -> discovery-only; construction moves to `array.add`.
8. UI: `app` -> `array`, splash handoff, scanner, focus (deauth + campaigns + PBC stop-the-world),
   focus_model, packet_dashboard, disconnect policy.
9. Full sweep for stragglers reading `active_interface` / `iface.<picture>`.

## Test migration

- `tests/wlan/test_interface.py:33,44` (AP signal averaging) -> `tests/wlan/test_sink.py`
  driven by `WlanSink.update` with `card_id`.
- `tests/models/test_models.py:5-8` (constructs `AccessPoint(signal=-50)`) -> assert via
  `signal_by_card` or through `WlanSink`, since `signal` is now a read-only property.
- New `test_dedupe.py`: two sources, same key inside/outside the window; novel vs dup vs both;
  N-source register/unregister.
- New `test_array.py`: `select_iface` channel filter + capability sort; array-of-one no-op
  dedupe; `_ingest` writes `signal_by_card` on novel and `record_signal` on dup;
  forged-MAC drop; hot_unplug re-emit counts.

## Verification

- `uv run pytest` green (new sink/dedupe/array suites + migrated model/interface tests).
- `uv run ruff check src/` clean (no `ruff format`).
- Hardware, single card (array-of-one): `uv run wifit3` splash -> scanner (APs populate,
  hopping) -> focus -> deauth, and a handshake/PMKID capture. RX health A/B:
  `beacon_watch.py` (live) vs `beacon_watch_usbcap.py` (capture count).
- Hardware, two cards: both add sequentially; scanner SPREAD covers more channels; deduped AP
  list has no cross-card duplicates; PBC stop-the-world runs; unplug one card -> toast, session
  survives; unplug last card -> splash.
- `scripts/cross_streams.py` and `scripts/ack_lab/*` still run (compat check for the raw per-card
  stream).

## Deferred (v2+)

- Stateful leasing / per-card dedication so scanning continues during a campaign.
- Concurrent campaigns (v1 is one at a time).
- Device loss mid-campaign beyond graceful abort (seamless re-election onto another card).
- TX-selection UI row (`Yes+TX` / `Yes RX-only` / `No`) at select + hotplug.
- Signal-based TX tie-break (pick the card with the strongest `signal_by_card` to the target).
