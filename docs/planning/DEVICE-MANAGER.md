# Device layer extraction (handoff)

Status: design finalized, revised against two full reviews (the flat-map blocker plus rev1's 11 and
rev2's 11 findings resolved inline). Implementation happens in a fresh session, so this doc is
self-contained. Read it top to bottom before touching code.

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
models/device_id.py       DeviceID (+ _SILICON)             (moved from chips/driver.py)
device/manager.py         the VID:PID map + family table + stateless queries + DeviceManager
device/watch.py           DeviceWatch                       (fires callbacks on plug-in / un-plug)
chips/<name>/__init__.py  SUPPORTED_IDS + import_driver      (per driver, added)
```

Import DAG (acyclic):
- `models/device_id.py` is a leaf module (a dataclass + `_SILICON`, zero internal imports). Note the
  light path imports it as `from wifit3.models.device_id import DeviceID`, not `from wifit3.models`,
  which re-exports the pydantic models. Importing any `models` submodule still runs `models/__init__.py`
  (a one-time pydantic load shared with the app's own `AccessPoint` use); if that startup cost matters,
  make the `models/__init__` re-exports lazy. It is not on the driver-import hot path either way.
- `chips/driver.py` imports `DeviceID` from `wifit3.models.device_id` and re-exports it (keeps every
  existing `from wifit3.chips.driver import DeviceID` working, see next section).
- `chips/<name>/__init__.py` imports `DeviceID` from `wifit3.models.device_id`.
- `wlan/interface.py` imports `Driver` + `FakeMacSupport` from `chips/driver` (NOT `DeviceID`).
- `device/manager.py` imports `DeviceID`, `WlanInterface` (wlan/interface), `setup.base`, and `chips/*`
  drivers lazily (inside `import_driver`, never at module top).
- `device/watch.py` imports `DeviceManager` and `DeviceID`.
- `ui/app.py` imports `DeviceManager`, `DeviceWatch`, `Status`, `WlanArray`.

The `device` <-> `setup` edge is acyclic: `device/manager` imports only `setup.base` at module top;
`setup/windows.py` and `setup/linux.py` import the device queries at their module top too, but those
two modules load only lazily via `Setup.for_platform()`, and `device/manager` never imports them.
`setup/__init__.py:36` (`target_for_vidpid`) is the one deferred in-function import.

## DeviceID -> `models/device_id.py`

Move `DeviceID` AND the `_SILICON` dict it reads (`chips/driver.py:30`, used by `DeviceID.silicon_vendor`
at `:58` and nowhere else) to `models/device_id.py`. Re-export `DeviceID` from `models/__init__.py`
(which already re-exports `AccessPoint`, `Client`, etc.), so general code can use `from wifit3.models
import DeviceID`.

Keep a runtime re-export in `chips/driver.py` (`from wifit3.models.device_id import DeviceID`). That
keeps the ~46 existing `from wifit3.chips.driver import DeviceID` sites (30 production including all 24
`driver.py`, which import it on a combined line with `Driver`/`FakeMacSupport`; 12 tests; 4 scripts)
working with no edit, so `pytest` stays green. Do NOT make the reference `TYPE_CHECKING`-only: that
would break all 46 sites at once.

## The VID:PID list lives in each driver's `__init__.py`

```python
# chips/rtl8812au/__init__.py
from wifit3.models.device_id import DeviceID

SUPPORTED_IDS = [DeviceID(0x0bda, 0x8812, "RTL8812AU", ...), ...]

def import_driver():
    from .driver import RTL8812AUDriver
    return RTL8812AUDriver
```

- `__init__.py` must NOT import `driver.py` at module top. `import_driver()` does the one heavy import,
  and only on a match. (Several `__init__.py` re-export the driver today; those top-level imports move
  into `import_driver`.)
- **`SUPPORTED_IDS` moves off the driver class.** In `chips/driver.py:110`, remove only `"SUPPORTED_IDS"`
  from the `__init_subclass__` tuple; KEEP `SUPPORTED_CHANNELS` (the ClassVar at `:74` and its guard, read
  at `interface.py:236` and `mt7921au/driver.py:71`). Delete the `SUPPORTED_IDS = [...]` block from each
  of the 24 `driver.py`. The list then exists in one place (`__init__.py`); setup reads the map, not
  `driver_cls.SUPPORTED_IDS`.
- A package with a `driver.py` but no `SUPPORTED_IDS` raises during the walk. The shared-base packages
  (`rtl88xxau_base`, `rtw88_base`) have no `driver.py` and are skipped.
- This shape must be documented in the porting process and scaffolded by `/port` (see Migration
  surface). It is a hard requirement: a newly-ported chip that omits it breaks discovery for everyone.

## The family table + the VID:PID map (the blocker fix)

A flat `{(vid,pid): import_driver}` map is wrong: five Realtek families ship TWO packages on the same or
overlapping VID:PID (e.g. `rtl8188eus` + `rtl8188eus_dkms` both `0x2357:0x010C`; the mainline package may
claim a superset, e.g. `rtw88_8814au` declares 15 IDs vs the dkms package's 1). `discovery.py` selects
per family via `WIFIT3_RTL*` (`env_or_none` at `:92-96`, the per-family calls at `:82-87`; default DKMS,
env flips to mainline). One slot per VID:PID cannot hold both, and it would drop the setup key.

A small central table in `device/manager.py` declares the families that need central handling: the two
single-package chips whose setup key differs from their dir (`ar9271` from `ar9271_v2`; `rtl8821cu` from
`rtl8821cu_dkms`) and the five dual-package families. Seven rows (re-audited: these seven are the ONLY
key != dir cases):

```
# device/manager.py  (data, not imported classes; env policy lifted from discovery.py:82-96)
_FAMILIES = [
  Family(key="ar9271",     default="ar9271_v2"),
  Family(key="rtl8821cu",  default="rtl8821cu_dkms"),
  Family(key="rtl8188eus", default="rtl8188eus_dkms", mainline="rtl8188eus",   env="WIFIT3_RTL8188"),
  Family(key="rtl8812au",  default="rtl8812au_dkms",  mainline="rtl8812au",    env="WIFIT3_RTL8812"),
  Family(key="rtl8821au",  default="rtl8821au_dkms",  mainline="rtl8821au",    env="WIFIT3_RTL8821"),
  Family(key="rtl8814au",  default="rtl8814au_dkms",  mainline="rtw88_8814au", env="WIFIT3_RTL8814"),
  Family(key="rtl8822bu",  default="rtl8822bu_dkms",  mainline="rtl8822bu",    env="WIFIT3_RTL8822"),
]
```

`default` is the always-chosen package (DKMS for the dual families, or the sole package). `mainline` is
the alternative, chosen only when `os.environ[env] == "mainline"`. Single-package rows leave
`mainline`/`env` unset. `ar9271` matters concretely: it uses Linux setup for real
(`ar9271_v2/driver.py:46`, `CONFLICTING_LINUX_MODULES=["ath9k_htc"]`), and `setup/linux.py:72-78` names
its blacklist/udev files by the key (`wifit3-ar9271.conf`, `60-wifit3-ar9271.rules`). A dir-derived key
would orphan a prior install's files on uninstall and leave the device blacklisted.

The map value carries the setup key: `supported_ids: {(vid,pid): (DeviceID, key, import_driver)}`.

The walk (pkgutil over `chips/*`, importing each light `__init__.py`):
- A package NOT in any family: `key` = its dir name; its `SUPPORTED_IDS` map straight in.
- A package IN a family: the family's selected package wins (`default`, or `mainline` when the env var
  says so). Only the winner's `SUPPORTED_IDS` populate the slot, under the family `key`. The loser's
  entries are skipped, so the two packages never collide. (Do NOT assume both packages declare identical
  IDs; the mainline may be a superset. Take the winner's list wholesale.)
- Assert no two NON-family packages claim the same `(vid,pid)`. CLAUDE.md documents driver-registry
  order as the disambiguation priority; a one-slot map has no such order (the walk is alphabetical). No
  such collision exists today (verified); the assert keeps a future one loud, and a real collision must
  be resolved by adding a family-table row with an explicit selection.

`driver_for(vid, pid)` and `target_for_vidpid(vid, pid)` return the `key`, so `uninstall` still resolves
the Linux files a prior install wrote. Default DKMS, the env flip, and every setup key are preserved:
this is a rename, not a behavior change.

## The queries are module-level; setup reaches them without an app

`setup/` needs the queries but has no `DeviceManager`: `Setup.install`/`uninstall` receive only a
Prompter, and `target_for_vidpid` is a free function. So the stateless surface is module-level.

- In `device/manager.py`, MODULE-level functions over one `functools.cache`'d map (one pkgutil walk,
  shared): `supported_ids()` (the map), `driver_for(vid,pid) -> (type[Driver], key)`, `device(id)`,
  `devices()`, `linux_node_path(id)`. These are the exact symbols `setup` imports and tests patch.
- `DeviceManager` (the class) is app-bound for `bringup`/`uninstall` and re-exposes the stateless
  queries as thin delegating methods, so the app and `DeviceWatch` can call `dm.devices()`.
- `driver_for` is the HEAVY helper: it calls `import_driver()`. The light scan (`devices`/`device`,
  polled every 0.5 s) must NOT route through it, or every poll re-imports drivers and loses the perf
  premise. The scan sources `(DeviceID, key, import_driver)` from `supported_ids()`; only build/bringup
  calls `driver_for`. (No external caller needs the DeviceID that the old `_match_driver` also returned;
  the two test callers take only `[0]`.)
- `device/manager` imports only `setup.base` at module top; the platform impls load lazily via
  `Setup.for_platform()`. No cycle.

## DeviceManager (`device/manager.py`)

Consolidates `discovery.py` (both halves) and `bringup.py`, and builds `WlanInterface` (see below for
why it is not wlan-agnostic). `Status` and `BringupResult` move here from `bringup.py`.

Module-level (stateless, over the cached map): `supported_ids()`, `driver_for(vid,pid) -> (type[Driver],
key)`, `device(id) -> DeviceID?`, `devices() -> list[DeviceID]`, `linux_node_path(id) -> str?`.

`DeviceManager` methods:

| name | returns | was |
|---|---|---|
| `devices()` / `device(id)` | (delegate to module) | `find_devices` / `find_device` |
| `wlan_iface(id, name)` | `WlanInterface?` | `build_interface` |
| `wlan_ifaces()` | `list[WlanInterface]` | `build_interfaces` |
| `wlan_close(ifaces)` | none | `close_interfaces` |
| `bringup(id, *, progress_cb, bail_at_permissions)` | `BringupResult` | `BringupManager.run` |
| `uninstall(id)` | `SetupResult` | `BringupManager.uninstall` |

Naming rule: a method leads with the type it returns. The home type (`DeviceID`) is bare (`devices`,
`device`); `WlanInterface` is the other type it produces, prefixed `wlan_`; a third return type names
itself (`driver_for`, `linux_node_path`).

Rules that must hold:
- **`devices()` contains the bus scan** (synchronous/blocking). There is NO `_scan_bus` helper.
  Everything that discovers a device dogfoods `devices()` / `device()`. `device(id)` = `devices()`
  filtered by VID:PID.
- `wlan_iface(id)` acquires the one live `usb.core.Device`. With `id.bus`/`id.address` set, a targeted
  `usb.core.find(idVendor, idProduct, bus, address)`; when they are None (a bare catalog DeviceID), keep
  the current first-VID:PID-match fallback (`discovery.py:178,182`), NOT `find(bus=None)` (matches
  nothing).
- `wlan_ifaces()` = `devices()` then `wlan_iface(each)`: N targeted finds instead of today's single
  `find_all`. Fine for the 1-3 device dev-script case; the one behavior delta.
- `linux_node_path(id)`: keep today's re-scan (`discovery.py:229-241`), byte-identical. It returns None
  when the tagged instance is not on the bus (`setup/linux.py:674` relies on that) and first-VID:PID
  matches a bare id. Do NOT format `id.bus`/`id.address` directly: a bare id has `None`, and
  `f"{None:03d}"` raises.
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

- `poll()` stays `async` and runs the blocking scan off the loop via `await asyncio.to_thread(dm.devices)`,
  then fires `on_change`. A synchronous `poll()` would run `usb.core.find` on the event loop every 0.5 s
  and stutter the TUI on the weak hardware this is for.
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
- **`WlanSink` stays private**, reached only through `WlanArray`'s methods (load-bearing for
  `campaigns/*`).
- **The prompter is UI-owned.** Bring-up's UI half (prompter open/close, `NewDeviceDialog`, result ->
  toast) lives in `ui/`. `DeviceManager.bringup` takes a `progress_cb` and returns a `BringupResult`. No
  UI in `device/` or `wlan/`.

## Scope (hard line)

- Only `wlan/`, `device/`, `models/`, and the per-driver `__init__.py`/`driver.py` change.
  `discovery.py`, `bringup.py`, `device_listener.py` are deleted.
- App changes are import paths + renames only: `self.bringup = BringupManager(self)` ->
  `self.device_manager = DeviceManager(self)`; `.run` -> `.bringup`; `self.devices = DeviceListener(...)`
  -> `self.device_watch = DeviceWatch(device_manager=..., ...)`; `poll_once` -> `poll`; `Status` import
  moves from `wlan.bringup` to `device.manager`.
- The `DeviceID` re-export in `chips/driver.py` keeps every step green.
- No behavior change, no new state, no `hasattr`/`getattr`, no app-orchestration changes. A real
  app-logic question stops the migration and gets raised, not papered over.

## Migration surface

Lists are illustrative; drive the repoint from a full-repo grep of each symbol AND its
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

*Tests:* patch-target repoints (`test_bringup`, `test_discovery`, `test_replug`, `test_device_listener`,
`test_hotplug`, `test_splash_bringup`, `test_bringup_error`, `test_setup_windows`, `test_setup_linux`,
`test_driver` rt2500usb + rtl8188eus_dkms), plus `tests/ui/test_error_modals.py:115` (`app.devices.resume`,
breaks on the rename) and the DeviceID-import tests (`tests/chips/test_device_id.py`,
`tests/ui/test_device_labels.py`).

*Required backstops (not asides):*
- `.github/workflows/ci.yml:48-51` imports `_import_driver_classes` for the import-smoke. Rewrite it to
  iterate `supported_ids()` and call every `import_driver()`, so the "every driver imports cleanly"
  check survives (the light walk alone would silently drop it).
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

NOT dead (earlier mislabeled): `DeviceWatch.present` (used at `splash.py:155`). The scan helper the old
`_match_driver` embodied is still needed, but it splits: the light `devices`/`device` scan reads
`supported_ids()`; the heavy `driver_for` (imports the driver) is build/bringup-only.

## Cross-cutting requirements

1. **Type `self.app` as the concrete app class.** Textual types `Screen.app` as the base `App`, so
   `self.app.device_watch` / `.device_manager` / `.array` do not resolve statically and the IDE reports
   real usages as unused (this hid `present()`). The `cast` form needs a runtime `WifiteApp` import
   (circular: `app.py` already imports the screens), and a real `app` property shadows Textual's
   inherited `Screen.app` descriptor. Use the type-only form per screen:
   `if TYPE_CHECKING: from wifit3.ui.app import WifiteApp` plus a bare annotation `app: WifiteApp` (no
   assignment). Touches every screen that reads app attributes (~30 `self.app.array` sites across splash,
   scanner, focus_v2).
2. **Comment style: effectively none.** Prefer zero comments and zero docstrings. One line only when a
   name genuinely cannot carry the meaning. No essayist docstrings, no restating the code, no history.
3. **Rename `self.devices` -> `self.device_watch`** (`app.py` def + its 4 sites; the 4 `splash.py` sites;
   `test_error_modals.py:115`).

## Gotchas

- `self.app.<attr>` chains across screens are invisible to static tools AND to a naive grep of the
  defining module. Verify every symbol with a full-repo grep of both `<instance>.<method>` and
  `self.app.<attr>.<method>`. Do not trust the IDE for "unused".
- `_match_driver` is used internally by the scan today, not dead.
- `DeviceWatch.present()` is live (splash).
- Keep the VID:PID map cache a module global so setup's access and every DeviceManager share one walk.

## Rollout order (each step ends with `pytest` green)

1. `models/device_id.py` (move `DeviceID` + `_SILICON` + re-export in `models/__init__.py`); add the
   runtime re-export in `chips/driver.py`. Green (re-export keeps all sites; nothing removed).
2. Add `SUPPORTED_IDS` + `import_driver` to all 24 `chips/<name>/__init__.py` (and move any top-level
   driver import into `import_driver`). Leave the `SUPPORTED_IDS` class attr on `driver.py` for now.
   Green (the additions are inert; nothing reads them yet).
3. **Cutover (one commit).** Create `device/manager.py` (family table, module-level cached map +
   queries, `DeviceManager`, `bringup`/`uninstall`, `Status`/`BringupResult`) and `device/watch.py`
   (`DeviceWatch`). Repoint every consumer in the same commit (`app.py`, `splash.py`, `setup/*`,
   `bringup_prompter.py`, scripts, tests, `ci.yml`), apply the `self.app` typing fix and the
   `self.devices` rename, and delete `discovery.py`, `bringup.py`, `device_listener.py`. This is
   necessarily atomic: those three are interdependent and split across the new modules, so nothing can be
   deleted before all consumers move. (Smaller commits are possible only with throwaway re-export shims;
   the atomic cutover avoids them.)
4. Remove the now-dead `SUPPORTED_IDS = [...]` block from the 24 `driver.py`, and drop only
   `"SUPPORTED_IDS"` from the `__init_subclass__` tuple. Green (nothing reads `driver_cls.SUPPORTED_IDS`
   after step 3).
5. Update `/port` + `docs/porting/*`.
6. Full-repo test-only sweep; gut confirmed dead code.
