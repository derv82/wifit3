# Device layer extraction (handoff)

Status: design finalized. Implementation happens in a fresh session, so this doc must be
self-contained. Read it top to bottom before touching code.

## Why (the point of all this)

Startup identifies the one plugged-in device by importing all 22 driver classes and scanning their
VID:PID lists: O(devices x drivers x ids). Reading a driver's list requires importing its `driver.py`,
which pulls the whole driver (transport, tx, rx, firmware, register tables). We pay ~200 ms+ to load
21 drivers we never use, and far more on the weak hardware this tool actually runs on.

Fix: read each driver's VID:PID list without importing the driver (a flat O(1) map), and import only
the one matched driver. The Linux modalias model: the VID:PID list is declared next to the driver,
stored in a light form, matched, and only then is the heavy code loaded.

### Measured (cold import, fresh interpreter each, min of 3)

| target | ms |
|---|---|
| floor (`chips.driver`: ABC + usb + asyncio) | 48 |
| one driver, median | 60 |
| one driver, heavy (`ar9271_v2`, `rtl8922au`) | 143 |
| all 22 (`discovery._import_driver_classes`) | 257 |
| all - one | 197 |
| all - floor | 209 |

~95 ms of the two heavy drivers is `libusb_package`, imported at their module top
(`ar9271_v2/driver.py:22`, `rtl8922au/driver.py:9`) but only used inside a method. Freebie: move the
import into the method.

## The principle

`wlan/` currently co-mingles three concerns: talking to USB **devices**, driving chipset **drivers**,
and **802.11** operations. This extraction pulls the device + driver-discovery concern out of `wlan/`
into a new top-level `wifit3/device/`. `wlan/` is left owning only 802.11 operations on
already-brought-up interfaces.

## Final structure

```
models/device_id.py       DeviceID                (moved from chips/driver.py)
device/manager.py         DeviceManager           (enumerate, driver_for, build, bring-up, teardown)
device/watch.py           DeviceWatch             (fires callbacks on device plug-in / un-plug)
chips/<name>/__init__.py  SUPPORTED_IDS + import_driver   (per driver, added)
```

Import DAG (no cycle):
- `models/device_id.py` is a leaf (a dataclass, zero internal imports).
- `chips/<name>/__init__.py` imports `DeviceID` from `wifit3.models`.
- `wlan/interface.py` imports `DeviceID` from `wifit3.models` (+ `chips/driver` for the `Driver` ABC).
- `device/manager.py` imports `DeviceID`, `wlan/interface.py` (`WlanInterface`), `chips/*` (drivers, lazily), `setup/`.
- `device/watch.py` imports `DeviceID` and holds a `DeviceManager` (to call `devices()`).
- `ui/app.py` imports `DeviceManager`, `DeviceWatch`, `wlan/array.py`.

## DeviceID -> `models/device_id.py`

`DeviceID` lives in `chips/driver.py` today. Move it to `models/device_id.py` and re-export from
`models/__init__.py` (which already re-exports `AccessPoint`, `Client`, etc.), so callers use
`from wifit3.models import DeviceID`. It is used broadly (the app, `DeviceManager`, `DeviceWatch`,
driver `__init__.py` files, `setup/`, `wlan/`), which is exactly why it belongs with the other
project-wide dataclasses, not buried under `chips/`. Repoint every `from wifit3.chips.driver import
DeviceID`. In `chips/driver.py`, reference `DeviceID` only in annotations (under `TYPE_CHECKING`) so
the ABC does not import it at runtime.

## The VID:PID list lives in each driver's `__init__.py`

```python
# chips/rtl8812au/__init__.py
from wifit3.models import DeviceID

SUPPORTED_IDS = [DeviceID(0x0bda, 0x8812, "RTL8812AU", ...), ...]

def import_driver():
    from .driver import RTL8812AUDriver
    return RTL8812AUDriver
```

- `__init__.py` must NOT import `driver.py` at module top. `import_driver()` does the one heavy
  import, and only on a match.
- This is a rule in the porting process (documented there; `/port` scaffolds the `__init__.py`). It is
  not type-enforced. A CI test that imports every driver package and asserts a well-formed
  `SUPPORTED_IDS` + `import_driver` is the optional backstop.
- A package with a `driver.py` but no `SUPPORTED_IDS` raises during the walk. The shared-base packages
  (`rtl88xxau_base`, `rtw88_base`) have no `driver.py` and are skipped.

## DeviceManager (`device/manager.py`)

Consolidates `discovery.py` (both halves) and `bringup.py`. It builds `WlanInterface` (see the next
section for why it is not wlan-agnostic).

| name | returns | was |
|---|---|---|
| `devices()` | `list[DeviceID]` | `find_devices` |
| `device(id)` | `DeviceID?` | `find_device` |
| `wlan_iface(id, name)` | `WlanInterface?` | `build_interface` |
| `wlan_ifaces()` | `list[WlanInterface]` | `build_interfaces` |
| `wlan_close(ifaces)` | none | `close_interfaces` |
| `driver_for(vid, pid)` | `type[Driver]` | `_match_driver` (was private) |
| `supported_ids` (property) | `{(vid,pid): (DeviceID, import_driver)}` | `_import_driver_classes` (was private) |
| `linux_node_path(id)` | `str?` | `usb_node_path` |
| `bringup(id, *, progress_cb, bail_at_permissions)` | `BringupResult` | `BringupManager.run` |
| `uninstall(id)` | `SetupResult` | `BringupManager.uninstall` |

Naming rule: a method leads with the type it returns. The home type (`DeviceID`) is bare (`devices`,
`device`); `WlanInterface` is the other type it produces, prefixed `wlan_` for legibility without
type-checking; a third return type names itself (`driver_for`, `supported_ids`, `linux_node_path`).

Rules that must hold:
- **`devices()` contains the bus scan.** There is NO `_scan_bus` helper. Everything that discovers a
  device dogfoods `devices()` / `device()`. `device(id)` = `devices()` filtered by VID:PID.
- `supported_ids` builds the flat map once (pkgutil walk over `chips/*`, importing each light
  `__init__.py`), cached.
- `driver_for(vid, pid)` reads the map and calls `import_driver()` on a hit. This is the only heavy
  import, and it happens once, on a match.
- `wlan_iface(id)` acquires the one live `usb.core.Device` with a targeted
  `usb.core.find(idVendor, idProduct, bus, address)`, then builds. That is a single-handle acquire,
  not a bus scan, so it does not reintroduce `_scan_bus`.
- `bringup()` = `wlan_iface` + connect (with setup-retry on `BringUpPermissionsError`) + attach to
  `app.array` + wire `notify_device_lost`. `setup.install` runs INSIDE `bringup()` on the permission
  branch; `uninstall()` is the standalone reverse. Both dispatch platform through `self.setup`
  (`Setup.for_platform()`), so the Windows/Linux branch stays in `setup/`.

### Why DeviceManager builds WlanInterface (not wlan-agnostic)

The OS model would put `WlanInterface` construction in the wlan layer (the kernel's device core is
network-agnostic; the driver registers with cfg80211/NDIS in probe). We rejected that here for a
concrete reason: **we do not know a device needs permissions until we try to `connect()`** (Windows
WinUSB is the base case: the failure only surfaces on connect). Bring-up is therefore the first moment
the permission path can run, and bring-up already needs to produce and attach a `WlanInterface`.
Splitting it would relocate connect + wrap + attach across the boundary for no gain and break the
drop-in scope. So `DeviceManager` reaches into `wlan/interface.py`, on purpose. It does NOT reach
`WlanArray` beyond the existing `_ensure_array` attach path (see scope).

## DeviceWatch (`device/watch.py`)

Fires callbacks on device plug-in / un-plug. Polled from outside (the app's Textual `set_interval`);
it holds the last-seen set and computes the delta. Not a manager, hence its own file.

| name | returns | note |
|---|---|---|
| `__init__(device_manager, on_change, on_fatal)` | | has-a `DeviceManager` |
| `poll()` | none | `dm.devices()` -> diff vs `_seen` -> `on_change(current, arrived, departed)` |
| `pause()` / `resume()` | none | frozen during a bring-up |
| `present()` | `list[DeviceID]` | the last-seen set (USED by splash, see map) |
| `wait_departure(id)` | `bool` (async) | `wait_for_departure` |
| `wait_arrival(id)` | `DeviceID?` (async) | `wait_for_arrival` |

State: `_seen`, `_paused`, `_stopped` (latched after a fatal so it is not re-reported each tick).
Fires `on_change(current, arrived, departed)` and `on_fatal(err)`.

## What stays put (do NOT move or change)

- **`WlanArray` stays app-owned** (`app.array`). It is the app's 802.11 operations surface, used
  directly by all of `campaigns/*` and the UI. `DeviceManager` reaches it only through the existing
  `_ensure_array` attach path, verbatim.
- **`WlanSink` stays private**, reached only through `WlanArray`'s methods. That facade is load-bearing
  for `campaigns/*`; it stays.
- **The prompter is UI-owned.** Bring-up's UI half (prompter open/close, `NewDeviceDialog`,
  result -> toast) lives in `ui/`. `DeviceManager.bringup` takes a `progress_cb` and returns a
  `BringupResult`. No UI in `device/` or `wlan/`.

## Scope (hard line)

- Only `wlan/`, `device/`, and the `DeviceID` move (`chips/driver.py` -> `models/`) change. `discovery.py`,
  `bringup.py`, and `device_listener.py` become **behavior-identical** and are deleted.
- App changes are import paths + renames only: `self.bringup = BringupManager(self)` ->
  `self.device_manager = DeviceManager(self)`; `.run` -> `.bringup`; `self.devices = DeviceListener(...)`
  -> `self.device_watch = DeviceWatch(device_manager=..., ...)`.
- No behavior change, no new state, no `hasattr`/`getattr`, no app-orchestration changes. A real
  app-logic question stops the migration and gets raised, not papered over.

## Migration surface (every caller to repoint)

*Production:*
- `device/watch.py` -> `dm.devices()` (was `device_listener` -> `find_devices`)
- `setup/windows.py` -> `device(id)` (was `find_device`)
- `setup/linux.py` -> `linux_node_path(id)` (was `usb_node_path`)
- `setup/__init__.py` -> `supported_ids` / `driver_for` (was `_import_driver_classes`)
- `ui/bringup_prompter.py` -> `app.device_watch.wait_arrival/wait_departure` (was `discovery.wait_for_*`)
- `ui/app.py` -> `DeviceManager`, `DeviceWatch`; `self.device_manager`, `self.device_watch`; timer ->
  `device_watch.poll`; `pause`/`resume`
- `ui/screens/splash.py` -> `app.device_manager.bringup/uninstall` (was `app.bringup.run/uninstall`),
  `Status`; `app.device_watch.present/pause/resume` (was `app.devices.*`, at splash.py:154,155,284,293)

*Scripts:* ~8 general (`wps_probe`, `pbc_probe`, `wep_lab`, `tx_retries`, `rx_autoack`,
`baseline_wifit3`, `soak`, `soak_all`) use `wlan_ifaces`/`wlan_close`/`devices`; 4 chip scripts use
`wlan_iface`.

*Tests (~10) that monkeypatch these symbols, repoint the patch targets to `device/manager` +
`device/watch`:* `test_bringup`, `test_discovery`, `test_replug`, `test_device_listener`,
`test_hotplug`, `test_splash_bringup`, `test_bringup_error`, `test_setup_windows`, `test_setup_linux`,
`test_driver` (rt2500usb, rtl8188eus_dkms).

### Side-effects the drop-in must carry verbatim

`bringup` creates `app.array` (`_ensure_array`), attaches the interface, wires
`app.notify_device_lost`, calls `setup.install`/`uninstall`, and drives the prompter. `DeviceWatch`
holds `_seen`/`_paused`/`_stopped` and fires `on_change`/`on_fatal`.

## Dead-code + test-only sweep (last step)

Run a FULL-REPO grep for each symbol (not the IDE, not a single-module read: see Gotchas). Anything in
`device/` reachable only from tests is either dead or a mis-migration.

Candidates already found (grep shows no production caller):
- `WlanArray.add`, `WlanArray.hotplug`, `WlanArray.hot_unplug` (the app brings up via `bringup`, never
  `WlanArray.add`).
- `WlanArray.register_rx_callback`, `WlanArray.unregister_rx_callback` (the code's own note: "no v1
  consumer, kept for future").

These live in `wlan/array.py`, not in the `device/` move. Removing them is a clean follow-up; decide
whether to fold it into this pass.

NOT dead (earlier mislabeled, corrected): `DeviceWatch.present` (used at `splash.py:155`);
`driver_for`/`_match_driver` (used internally by the scan at `discovery.py:146`).

## Cross-cutting requirements

1. **Type `self.app` as the concrete app class.** Textual types `Screen.app` as the base `App`, so
   `self.app.device_watch` / `.device_manager` / `.array` do not resolve statically and the IDE reports
   real usages as unused (this is what hid `present()`). Annotate the screens' `app` as `WifiteApp`
   (a typed property or a `cast`) so static analysis follows screen -> app access.
2. **Comment style: effectively none.** Prefer zero comments and zero docstrings. One line only when a
   name genuinely cannot carry the meaning. No essayist docstrings, no restating the code, no history.
   `discovery.py` today is ~270 lines, roughly half of it docstrings: do not carry that across.
3. **Rename `self.devices` -> `self.device_watch`** (`app.py` def + the four `splash.py` sites). The old
   name did not match its class.

## Gotchas

- `self.app.<attr>` chains across screens are invisible to static tools AND to a naive grep of the
  defining module. Verify every symbol's usage with a full-repo grep of both `<instance>.<method>` and
  `self.app.<attr>.<method>`. Do not trust the IDE for "unused".
- `_match_driver` is an internal helper of the scan, not dead.
- `DeviceWatch.present()` is live (splash).

## Rollout order

1. `models/device_id.py` (move + re-export + repoint `from wifit3.chips.driver import DeviceID`). Add
   `SUPPORTED_IDS` + `import_driver` to one driver's `__init__.py` per family (Realtek / MediaTek /
   Ralink / Atheros).
2. `device/manager.py` `DeviceManager` (enumerate / `driver_for` / `supported_ids` / `wlan_iface` /
   `wlan_ifaces` / `wlan_close` / `linux_node_path`) + the pkgutil walk. Lift discovery out.
3. `device/manager.py` `bringup`/`uninstall` (from `bringup.py`); `device/watch.py` `DeviceWatch`
   (from `device_listener.py` + `wait_*`). Repoint `app.py`, `splash.py`, `setup/*`,
   `bringup_prompter.py`. Apply the `self.app` typing fix and the `self.devices` rename.
4. Migrate the remaining driver `__init__.py` files.
5. Repoint scripts + tests.
6. Delete `discovery.py`, `bringup.py`, `device_listener.py`.
7. Full-repo test-only sweep; gut confirmed dead code.
