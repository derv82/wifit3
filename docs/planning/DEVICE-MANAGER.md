# Device layer extraction (handoff)

Status: design finalized, revised against a full review (the flat-map blocker and 11 other findings
resolved inline). Implementation happens in a fresh session, so this doc is self-contained. Read it
top to bottom before touching code.

## Why (the point of all this)

Startup identifies the one plugged-in device by importing all 24 driver classes and scanning their
VID:PID lists: O(devices x drivers x ids). Reading a driver's list requires importing its `driver.py`,
which pulls the whole driver (transport, tx, rx, firmware, register tables). We pay ~200 ms+ to load
23 drivers we never use, and far more on the weak hardware this tool actually runs on.

Fix: read each driver's VID:PID list without importing the driver (a cached map), and import only the
one matched driver. The Linux modalias model: the VID:PID list is declared next to the driver, stored
in a light form, matched, and only then is the heavy code loaded.

### Measured (cold import, fresh interpreter each, min of 3)

| target | ms |
|---|---|
| floor (`chips.driver`: ABC + usb + asyncio) | 48 |
| one driver, median | 60 |
| one driver, heavy (`ar9271_v2`, `rtl8922au`) | 143 |
| all 24 (`discovery._import_driver_classes`) | 257 |
| all - one | 197 |
| all - floor | 209 |

~95 ms of the two heavy drivers is `libusb_package`, imported at their module top
(`ar9271_v2/driver.py:22`, `rtl8922au/driver.py:9`) but only used inside a method. Freebie: move the
import into the method.

## The principle

`wlan/` today mixes three concerns: talking to USB **devices**, driving chipset **drivers**, and
**802.11** operations. This extraction pulls the device + driver-discovery concern out of `wlan/` into
a new top-level `wifit3/device/`. `wlan/` is left owning only 802.11 operations on already-brought-up
interfaces.

## Final structure

```
models/device_id.py       DeviceID                          (moved from chips/driver.py)
device/manager.py         DeviceManager + the VID:PID map + the family table
device/watch.py           DeviceWatch                       (fires callbacks on plug-in / un-plug)
chips/<name>/__init__.py  SUPPORTED_IDS + import_driver      (per driver, added)
```

Import DAG (acyclic):
- `models/device_id.py` is a leaf (a dataclass, zero internal imports).
- `chips/driver.py` imports `DeviceID` from `wifit3.models` and re-exports it (keeps every existing
  `from wifit3.chips.driver import DeviceID` working, see next section).
- `chips/<name>/__init__.py` imports `DeviceID` from `wifit3.models`.
- `wlan/interface.py` imports `Driver` + `FakeMacSupport` from `chips/driver` (NOT `DeviceID`).
- `device/manager.py` imports `DeviceID` (models), `WlanInterface` (wlan/interface), `setup.base`, and
  `chips/*` drivers lazily (inside `import_driver`, never at module top).
- `device/watch.py` imports `DeviceManager` and `DeviceID`.
- `setup/*` imports `device/manager`'s module-level queries LAZILY (deferred, inside functions), so the
  `device` <-> `setup` edge never closes a cycle.
- `ui/app.py` imports `DeviceManager`, `DeviceWatch`, `Status`, `WlanArray`.

## DeviceID -> `models/device_id.py`

Move `DeviceID` from `chips/driver.py` to `models/device_id.py` and re-export from `models/__init__.py`
(which already re-exports `AccessPoint`, `Client`, etc.), so new code uses `from wifit3.models import
DeviceID`. It is used broadly (the app, `DeviceManager`, `DeviceWatch`, driver `__init__.py`, `setup/`,
`wlan/`), which is why it belongs with the other project-wide dataclasses, not under `chips/`.

Keep a runtime re-export in `chips/driver.py` (`from wifit3.models import DeviceID`; models is a leaf,
no cycle). That keeps the ~46 existing `from wifit3.chips.driver import DeviceID` sites (30 production
including all 24 `driver.py`, which import it on a combined line with `Driver`/`FakeMacSupport`; 12
tests; 4 scripts) working with no edit, so `pytest` stays green through every migration step. Do NOT
make the reference `TYPE_CHECKING`-only: that would break all 46 sites at once.

## The VID:PID list lives in each driver's `__init__.py`

```python
# chips/rtl8812au/__init__.py
from wifit3.models import DeviceID

SUPPORTED_IDS = [DeviceID(0x0bda, 0x8812, "RTL8812AU", ...), ...]

def import_driver():
    from .driver import RTL8812AUDriver
    return RTL8812AUDriver
```

- `__init__.py` must NOT import `driver.py` at module top. `import_driver()` does the one heavy import,
  and only on a match.
- **`SUPPORTED_IDS` moves off the driver class.** Drop the `Driver` ABC requirement (`chips/driver.py:71`
  + the `__init_subclass__` check at `:108-112`) and delete the `SUPPORTED_IDS = [...]` block from each
  of the 24 `driver.py`. The list then exists in exactly one place (`__init__.py`). Setup reads the map,
  not `driver_cls.SUPPORTED_IDS`.
- A package with a `driver.py` but no `SUPPORTED_IDS` raises during the walk. The shared-base packages
  (`rtl88xxau_base`, `rtw88_base`) have no `driver.py` and are skipped.
- This shape must be documented in the porting process and scaffolded by `/port` (see Migration
  surface). It is a hard requirement, not an aside: a newly-ported chip that omits it breaks discovery
  for every user.

## The family table + the VID:PID map (the blocker fix)

A flat `{(vid,pid): import_driver}` map is wrong: five Realtek families ship TWO packages on the SAME
VID:PID (e.g. `rtl8188eus` + `rtl8188eus_dkms` both `0x2357:0x010C`), and `discovery.py:82-88` picks
per family via `WIFIT3_RTL*` (default DKMS, env flips to mainline). One slot per VID:PID cannot hold
both, and it would drop the setup key. So the map keeps the family and the selection.

A small central table in `device/manager.py` declares the families that need central handling: the
five dual-package families plus `rtl8821cu` (single package, but its setup key `rtl8821cu` differs from
its dir `rtl8821cu_dkms`). Six rows:

```
# device/manager.py  (data, not imported classes; env policy lifted verbatim from discovery.py:65-88)
_FAMILIES = [
  Family(key="rtl8188eus", env="WIFIT3_RTL8188", mainline="rtl8188eus",   dkms="rtl8188eus_dkms"),
  Family(key="rtl8812au",  env="WIFIT3_RTL8812", mainline="rtl8812au",    dkms="rtl8812au_dkms"),
  Family(key="rtl8821au",  env="WIFIT3_RTL8821", mainline="rtl8821au",    dkms="rtl8821au_dkms"),
  Family(key="rtl8814au",  env="WIFIT3_RTL8814", mainline="rtw88_8814au", dkms="rtl8814au_dkms"),
  Family(key="rtl8822bu",  env="WIFIT3_RTL8822", mainline="rtl8822bu",    dkms="rtl8822bu_dkms"),
  Family(key="rtl8821cu",  env=None,             mainline=None,           dkms="rtl8821cu_dkms"),
]
```

The map value carries the setup key: `supported_ids: {(vid,pid): (DeviceID, key, import_driver)}`.

The walk (pkgutil over `chips/*`, importing each light `__init__.py`):
- A package NOT in any family: `key` = its dir name; its `SUPPORTED_IDS` map straight in.
- A package IN a family: the family's env-selected package wins (default DKMS; `WIFIT3_RTL*="mainline"`
  flips). Only the winner's `SUPPORTED_IDS` populate the slot, under the family `key`. The loser's
  entries are skipped, so the two packages never collide. `env_or_none` moves verbatim.

`driver_for(vid, pid)` and `target_for_vidpid(vid, pid)` return the `key`, so `uninstall` still resolves
the Linux blacklist/udev files a prior install wrote under it. Default DKMS, the env flip, and the key
provenance are all preserved: this is a rename, not a behavior change.

## Setup reaches the map without an app

`setup/` needs the queries but has no `DeviceManager`: `Setup.install`/`uninstall` receive only a
Prompter, and `target_for_vidpid` is a free function. So:

- The VID:PID map and the stateless queries (`devices`, `device`, `driver_for`, `supported_ids`,
  `linux_node_path`) are backed by a MODULE-level cached build in `device/manager.py`: one pkgutil
  walk, shared by every caller. `functools.cache` on the builder (hot-reload of drivers is a non-goal).
- Only `bringup`/`uninstall` are app-bound. The stateless queries do not touch the app.
- `setup` keeps its existing deferred import (`setup/__init__.py:36` already does this), now pointing at
  the module-level query, so it needs no `DeviceManager` instance and there is no second walk.
- `device/manager` imports only `setup.base` at module top; the platform impls load lazily via
  `Setup.for_platform()`, as today. No cycle.

## DeviceManager (`device/manager.py`)

Consolidates `discovery.py` (both halves) and `bringup.py`. It builds `WlanInterface` (see the next
section for why it is not wlan-agnostic). `Status` and `BringupResult` move here from `bringup.py`.

| name | returns | was |
|---|---|---|
| `devices()` | `list[DeviceID]` | `find_devices` |
| `device(id)` | `DeviceID?` | `find_device` |
| `wlan_iface(id, name)` | `WlanInterface?` | `build_interface` |
| `wlan_ifaces()` | `list[WlanInterface]` | `build_interfaces` |
| `wlan_close(ifaces)` | none | `close_interfaces` |
| `driver_for(vid, pid)` | `(type[Driver], key)` | `_match_driver` (was private) |
| `supported_ids` (property) | `{(vid,pid): (DeviceID, key, import_driver)}` | `_import_driver_classes` (was private) |
| `linux_node_path(id)` | `str?` | `usb_node_path` |
| `bringup(id, *, progress_cb, bail_at_permissions)` | `BringupResult` | `BringupManager.run` |
| `uninstall(id)` | `SetupResult` | `BringupManager.uninstall` |

Naming rule: a method leads with the type it returns. The home type (`DeviceID`) is bare (`devices`,
`device`); `WlanInterface` is the other type it produces, prefixed `wlan_`; a third return type names
itself (`driver_for`, `supported_ids`, `linux_node_path`).

Rules that must hold:
- **`devices()` contains the bus scan** (synchronous/blocking). There is NO `_scan_bus` helper.
  Everything that discovers a device dogfoods `devices()` / `device()`. `device(id)` = `devices()`
  filtered by VID:PID.
- `wlan_iface(id)` acquires the one live `usb.core.Device`. When `id.bus`/`id.address` are set, a
  targeted `usb.core.find(idVendor, idProduct, bus, address)`; when they are None (a bare catalog
  DeviceID), keep the current first-VID:PID-match fallback (`discovery.py:178,182`), NOT
  `find(bus=None)`, which matches nothing.
- `wlan_ifaces()` = `devices()` then `wlan_iface(each)`: N targeted finds instead of today's single
  `find_all`. Fine for the 1-3 device dev-script case; noted as the one behavior delta.
- `linux_node_path(id)` runs off `id.bus`/`id.address` directly, no handle acquire.
- `bringup()` = `wlan_iface` + connect (with setup-retry on `BringUpPermissionsError`) + attach to
  `app.array` + wire `notify_device_lost`. `setup.install` runs INSIDE `bringup()` on the permission
  branch; `uninstall()` is the standalone reverse. Both dispatch platform through `self.setup`
  (`Setup.for_platform()`).
- Carry `_name_counter` / `_next_name` (`bringup.py:58,126-129`): interfaces are numbered `wlan0..N`
  monotonically across the app's lifetime. Resetting per call collides names.

### Why DeviceManager builds WlanInterface (not wlan-agnostic)

The OS model would put `WlanInterface` construction in the wlan layer (the kernel's device core is
network-agnostic; the driver registers with cfg80211/NDIS in probe). We rejected that here for a
concrete reason: **we do not know a device needs permissions until we try to `connect()`** (Windows
WinUSB is the base case: the failure only surfaces on connect). Bring-up is the first moment the
permission path can run, and it already needs to produce and attach a `WlanInterface`. Splitting it
relocates connect + wrap + attach across the boundary for no gain and breaks the drop-in scope. So
`DeviceManager` reaches `wlan/interface.py`, on purpose. It does NOT reach `WlanArray` beyond the
existing `_ensure_array` attach path.

## DeviceWatch (`device/watch.py`)

Fires callbacks on device plug-in / un-plug. Driven from outside (the app's Textual `set_interval`); it
holds the last-seen set and computes the delta. Not a manager, hence its own file.

| name | returns | note |
|---|---|---|
| `__init__(device_manager, on_change, on_fatal)` | | has-a `DeviceManager` |
| `poll()` | none (async) | `await asyncio.to_thread(dm.devices)` -> diff vs `_seen` -> `on_change` |
| `pause()` / `resume()` | none | frozen during a bring-up |
| `present()` | `list[DeviceID]` | the last-seen set (USED by splash, see map) |
| `wait_departure(id, *, timeout=120.0, interval=0.3)` | `bool` (async) | `wait_for_departure` |
| `wait_arrival(id, *, timeout=120.0, interval=0.3)` | `DeviceID?` (async) | `wait_for_arrival` |

- `poll()` stays `async` and runs the blocking scan off the loop via
  `await asyncio.to_thread(dm.devices)`, then fires `on_change`. A synchronous `poll()` would run
  `usb.core.find` on the event loop every 0.5 s and stutter the TUI on the weak hardware this is for.
- `wait_*` keep the `timeout`/`interval` kwargs (`bringup_prompter.py:57-58` relies on the 120 s bound;
  `test_replug.py` passes them at 7 sites). They compute their OWN fresh baseline through `dm.devices()`
  (NOT `_seen`), and are valid only inside a `pause()` window. Wiring them to the frozen `_seen` makes
  the replug wait read a stale set and hang to the timeout.
- State: `_seen`, `_paused`, `_stopped` (latched after a fatal). Fires `on_change(current, arrived,
  departed)` and `on_fatal(err)`.

## What stays put (do NOT move or change)

- **`WlanArray` stays app-owned** (`app.array`), the app's 802.11 operations surface, used directly by
  all of `campaigns/*` and the UI. `DeviceManager` reaches it only through the existing `_ensure_array`
  attach path, verbatim.
- **`WlanSink` stays private**, reached only through `WlanArray`'s methods. That facade is load-bearing
  for `campaigns/*`; it stays.
- **The prompter is UI-owned.** Bring-up's UI half (prompter open/close, `NewDeviceDialog`, result ->
  toast) lives in `ui/`. `DeviceManager.bringup` takes a `progress_cb` and returns a `BringupResult`. No
  UI in `device/` or `wlan/`.

## Scope (hard line)

- Only `wlan/`, `device/`, `models/` (the `DeviceID` move), and the per-driver `__init__.py` change.
  `discovery.py`, `bringup.py`, `device_listener.py` become behavior-identical and are deleted.
- App changes are import paths + renames only: `self.bringup = BringupManager(self)` ->
  `self.device_manager = DeviceManager(self)`; `.run` -> `.bringup`; `self.devices = DeviceListener(...)`
  -> `self.device_watch = DeviceWatch(device_manager=..., ...)`; `poll_once` -> `poll`; the
  `from wifit3.wlan.bringup import Status` -> `from wifit3.device.manager import Status`.
- The `DeviceID` re-export in `chips/driver.py` keeps every step green.
- No behavior change, no new state, no `hasattr`/`getattr`, no app-orchestration changes. A real
  app-logic question stops the migration and gets raised, not papered over.

## Migration surface

The lists below are illustrative; drive the actual repoint from a full-repo grep of each symbol AND its
`self.app.<attr>.<method>` chains (static tools and single-module greps miss those, see Gotchas).

*Production:*
- `device/watch.py` -> `dm.devices()` (was `device_listener` -> `find_devices`)
- `setup/windows.py` -> `device(id)`; `setup/linux.py` -> `linux_node_path(id)`; `setup/__init__.py` ->
  `supported_ids` / `driver_for` / `target_for_vidpid` (was `_import_driver_classes`)
- `ui/bringup_prompter.py` -> `app.device_watch.wait_arrival/wait_departure`
- `ui/app.py` -> `DeviceManager`, `DeviceWatch`, `Status` (`:10,:166,:168`); `self.device_manager`,
  `self.device_watch`; the 4 `self.devices` sites (`:127` def, `:142/:143` poll, `:154` pause, `:171`
  resume); timer -> `device_watch.poll`
- `ui/screens/splash.py` -> `app.device_manager.bringup/uninstall`, `Status`;
  `app.device_watch.present/pause/resume` (`:154,155,284,293`)

*Scripts (grep-confirmed: 9 general + 5 chip):* `wps_probe`, `pbc_probe`, `wep_lab`, `tx_retries`,
`rx_autoack`, `baseline_wifit3`, `soak`, `soak_all`, `rx/beacon_watch`; chip scripts
`rtl8188eus_dkms/bringup_repro`, `mt7925au/set_channel_probe`, `rtl8821cu_dkms/warm_reattach_repro`,
`rtl8821cu_dkms/prime_ablation`, `rtl8922au/_amlib`.

*Tests:* the monkeypatch/patch-target repoints (`test_bringup`, `test_discovery`, `test_replug`,
`test_device_listener`, `test_hotplug`, `test_splash_bringup`, `test_bringup_error`,
`test_setup_windows`, `test_setup_linux`, `test_driver` rt2500usb + rtl8188eus_dkms), plus
`tests/ui/test_error_modals.py:115` (`app.devices.resume`, breaks on the rename), and the DeviceID-import
tests (`tests/chips/test_device_id.py`, `tests/ui/test_device_labels.py`).

*Required backstops (not asides):*
- `.github/workflows/ci.yml:48-51` imports `_import_driver_classes` for the import-smoke. Deleting
  `discovery.py` reddens CI. Rewrite the smoke to iterate `supported_ids` and call every
  `import_driver()`, so the "every driver imports cleanly" check survives (the light walk alone would
  silently drop it).
- `.claude/skills/port/` + `docs/porting/*`: add the `__init__.py` `SUPPORTED_IDS` + `import_driver`
  requirement and have `/port` scaffold it.

### Side-effects the drop-in must carry verbatim

`bringup` creates `app.array` (`_ensure_array`), attaches the interface, wires `app.notify_device_lost`,
calls `setup.install`/`uninstall`, drives the prompter, and holds `_name_counter`/`_next_name`.
`DeviceWatch` holds `_seen`/`_paused`/`_stopped` and fires `on_change`/`on_fatal`.

## Dead-code + test-only sweep (last step)

Run a FULL-REPO grep for each symbol (not the IDE, not a single-module read: see Gotchas). Anything in
`device/` reachable only from tests is either dead or a mis-migration.

Candidates already found (grep shows no production caller):
- `WlanArray.add`, `WlanArray.hotplug`, `WlanArray.hot_unplug` (the app brings up via `bringup`, never
  `WlanArray.add`).
- `WlanArray.register_rx_callback`, `WlanArray.unregister_rx_callback` ("no v1 consumer, kept for
  future").

These live in `wlan/array.py`, not the `device/` move. Fold their removal into this pass or defer it,
your call.

NOT dead (earlier mislabeled): `DeviceWatch.present` (used at `splash.py:155`); `driver_for` /
`_match_driver` (used internally by the scan at `discovery.py:146`).

## Cross-cutting requirements

1. **Type `self.app` as the concrete app class.** Textual types `Screen.app` as the base `App`, so
   `self.app.device_watch` / `.device_manager` / `.array` do not resolve statically and the IDE reports
   real usages as unused (this is what hid `present()`). The `cast` form needs a runtime `WifiteApp`
   import (circular: `app.py` already imports the screens), and a real `app` property shadows Textual's
   inherited `Screen.app` descriptor. Use the type-only form per screen:
   `if TYPE_CHECKING: from wifit3.ui.app import WifiteApp` plus a bare annotation `app: WifiteApp` (no
   assignment). Touches every screen that reads app attributes (~30 `self.app.array` sites across splash,
   scanner, focus_v2).
2. **Comment style: effectively none.** Prefer zero comments and zero docstrings. One line only when a
   name genuinely cannot carry the meaning. No essayist docstrings, no restating the code, no history.
3. **Rename `self.devices` -> `self.device_watch`** (`app.py` def + its 4 sites; the 4 `splash.py` sites;
   `test_error_modals.py:115`). The old name did not match its class.

## Gotchas

- `self.app.<attr>` chains across screens are invisible to static tools AND to a naive grep of the
  defining module. Verify every symbol with a full-repo grep of both `<instance>.<method>` and
  `self.app.<attr>.<method>`. Do not trust the IDE for "unused".
- `_match_driver` is an internal helper of the scan, not dead.
- `DeviceWatch.present()` is live (splash).
- The registry cache is a module global; keep it that way so setup's access and the app's DeviceManager
  share one pkgutil walk.

## Rollout order (each step keeps `pytest` green)

1. `models/device_id.py` (move + re-export in `models/__init__.py`); add the runtime re-export in
   `chips/driver.py`; drop the `SUPPORTED_IDS` ABC requirement.
2. `device/manager.py`: the family table, the module-level cached VID:PID map, and the stateless queries
   (`devices` / `device` / `driver_for` / `supported_ids` / `linux_node_path`). Add
   `SUPPORTED_IDS` + `import_driver` to every driver `__init__.py` and delete the `SUPPORTED_IDS` block
   from each `driver.py`. Repoint `setup/*` (deferred import). Rewrite the ci.yml smoke. Update `/port`
   + `docs/porting/*`. Delete `discovery.py`.
3. `device/manager.py` `bringup`/`uninstall` + `Status`/`BringupResult` (from `bringup.py`);
   `device/watch.py` `DeviceWatch` (from `device_listener.py` + `wait_*`). Repoint `app.py`,
   `splash.py`, `bringup_prompter.py`. Apply the `self.app` typing fix and the `self.devices` rename.
   Delete `bringup.py`, `device_listener.py`.
4. Repoint the remaining scripts + tests (grep-driven).
5. Full-repo test-only sweep; gut confirmed dead code.
