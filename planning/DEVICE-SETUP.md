# Wifit3 — Device Setup (Zadig / udev) plan

How a non-technical user goes from "plugged the card in" to "wifit3 sees it,"
without a Zadig tutorial or a `sudo rmmod` incantation. This is the design for
RELEASE-PLAN § 2d.

## The immovable constraint

wifit3 talks to cards through libusb, so every card must be reachable by libusb:

- **Windows** — the device must be bound to **WinUSB** (Zadig's job today).
- **Linux** — the **kernel driver must release** the device (rmmod / unbind / detach).

Both are **privileged, one-time-ish actions**. We cannot delete the privileged
step — only (a) reduce it to a single clean prompt, (b) make it self-explaining,
and (c) make it reversible. Any plan that promises "zero elevation, fully bundled"
is lying; the honest goal is *one prompt, clearly explained, easy to undo*.

Second constraint, easy to forget: **binding the card to WinUSB (or unbinding it on
Linux) stops it being a normal Wi-Fi adapter.** That has to be *visible* and
*reversible*, or we've broken the user's internet to run a Wi-Fi tool.

## Tiers (ship in this order)

**Tier 0 — Detect & Diagnose. MVP, the only release gate.**
On discovery, classify each known VID:PID into `ready` / `present-but-unbound` /
`unknown`, and for `present-but-unbound` show the *exact* next step (copy-pasteable
command on Linux, one-line "needs WinUSB" on Windows). We elevate nothing. This is
cheap, cross-platform, and kills most of the "it doesn't work" support load on its
own, because the failure stops being silent. **Everything below is an upgrade, not
a gate.** *(Revised — the openability classification moved off the poll loop to
device-select time; see "Splash flow — revised to a select-time WinUSB check" below.)*

**Tier 1 — One-prompt assisted bind.**
Windows: bundled libwdi, relaunch the bind step elevated (UAC), install WinUSB for
the specific VID:PID, re-enumerate. Linux: `pkexec`-install a udev rules file once;
no per-run sudo after.

**Tier 2 — In-TUI flow + revert.**
A wizard inside the app (detect → "Set up [card]" → elevate → verify → done) plus a
"Restore [card] to normal Wi-Fi" action. Post-alpha.

## Windows (WinUSB)

- **Mechanism.** libusb needs WinUSB (or libusbK) on the device. **Zadig is a GUI
  over `libwdi`**, libusb's driver-installer library. libwdi ships `wdi-simple.exe`,
  a CLI that generates + installs a WinUSB INF for a given VID:PID — *this is the
  automation lever* (call the exe, or libwdi via ctypes). No manual Zadig.
- **Elevation.** Relaunch only the bind step elevated via `ShellExecuteW(..,"runas",..)`
  → UAC. The wifit3 runtime itself still needs no admin once bound (README already
  states this).
- **Reversion.** WinUSB binding persists across reboots; restoring the card as a
  normal adapter means uninstalling the WinUSB driver (Device Manager → uninstall
  device + delete driver → rescan, or Zadig "revert"). This **must** be a
  documented/automated "Restore" action — it's the easiest footgun in the project.
- **Bundling cost.** `wdi-simple.exe` + libusb DLL, per-arch (x64 / arm64). Adds
  binary deps to the wheel/installer.

### [VERIFY-W1] Can libusb even *see* an unbound device? ⚠ decides Tier 0 on Windows

§2d assumes "detect known VID:PID via `usb.core`." That may be false on Windows: the
bundled libusb backend may only enumerate devices that already have a libusb-class
driver. If a freshly-plugged, WinUSB-less card **doesn't appear** in
`usb.core.find(find_all=True)`, then Tier-0 detection needs a *parallel* enumeration
that sees all USB regardless of driver — SetupAPI, WMI `Get-PnpDevice`, or
`pnputil /enum-devices` — matched against the SUPPORTED_IDS list, then diffed against
what libusb can open. **Test:** plug an un-bound card, run `usb.core.find`, see if the
VID:PID shows. This is the single biggest unknown in the Windows path.

> **RESOLVED — the easy direction.** Verified on a fresh RT3070 (AWUS036NH,
> `148f:3070`) still on its native Wi-Fi driver (`Class=Net`, `Service=netr28ux`,
> never Zadig'd). `usb.core.find(find_all=True, backend=libusb_package…)` **does
> enumerate it** unbound — so Tier-0 detection needs **no** parallel SetupAPI/WMI
> path; plain `find()` sees every card regardless of driver. The
> *present-but-unbound* signal is the **open** step: `find()` succeeds, but any
> operation that opens the device (`get_active_configuration`, `ctrl_transfer`,
> `get_string`) raises `NotImplementedError` *"Operation not supported or
> unimplemented on this platform"* (errno=None) — libusb `LIBUSB_ERROR_NOT_SUPPORTED`,
> i.e. enumerable but no WinUSB driver to open it. Classifier: match `SUPPORTED_IDS`
> from `find()`, then try-open → success = `ready`, `NotImplementedError` =
> `present-but-unbound`, else `unknown`. Catch on the exception type + the
> found-but-can't-open *pattern*, not the message string.

### [VERIFY-W2] libwdi signing on current Win10/11

WinUSB is inbox + WHQL-signed, and Zadig works for everyone without the user signing
anything, so libwdi handles the INF/catalog. Confirm there's no SmartScreen / driver-
signature friction when *we* invoke `wdi-simple.exe` from a bundled (possibly
unsigned) app.

## Tier 1 — Windows implementation (decided plan, 2026-06)

Tier 0 shipped (commit 09cefd7): the classifier, the splash surfacing, and a stub Install
button. Tier 1 makes that button *bind* the card and adds an in-app revert.

**Sourcing `wdi-simple.exe` — we build it ourselves.** There is no official prebuilt
binary, and the libwdi maintainer [won't publish one](https://github.com/pbatard/libwdi/issues/309)
— explicitly because distributing an *unsigned, elevation-requiring* exe is, in his words,
"VERY BAD PRACTICE." The build lever is libwdi's `vs2022.yml` GitHub Action: it compiles
`wdi-simple.exe` (x64 + Win32, with the WinUSB redist embedded → effectively standalone, no
loose DLLs) and uploads it as the `VS2022` artifact on every push. Upstream artifacts expire
(90-day retention — the most recent is already gone), so we build on a **disposable private
mirror** pinned to a release. Pinned: **v1.5.1 (`9b23b82`)**. That workflow builds x64/Win32
only — **arm64 Windows stays unsolved** (open question below). (A *fork* of a public repo
can't be made private; we use a private *mirror* and delete it after extracting — provenance
doesn't depend on the build repo surviving.)

**Provenance, since we can't sign it.** The honest answer to the maintainer's warning:
record, in `setup/bin/PROVENANCE.md`, the upstream tag + commit SHA + the exe's SHA-256 (the
workflow already prints `sha256sum`). Anyone can rebuild from that commit and compare — the
binary is auditable even though unsigned. Signing our *own* artifact (posture B: libwdi-as-a-
library inside a signed wifit3 helper) is the post-alpha upgrade. WCID — the maintainer's
other suggestion — is **out**: it needs MS-OS descriptors baked into the *device* firmware,
which we don't manufacture.

**Invocation.** `setup/windows.py: install_winusb(vid, pid, iid=0, name=...)` →
`ShellExecuteW(.., "runas", ..)` (one UAC prompt — driver install is inherently privileged)
→ `WaitForSingleObject` + `GetExitCodeProcess`. Exit code is the WDI enum (UAC-cancel is its
own code → "you cancelled," not "it failed"); stdout can't cross the elevation boundary, so
we don't parse it. Runs in a Textual `@work` thread (mirrors `perform_connect`) so UAC
doesn't block the loop; on success re-run `WlanDeviceManager.refresh()` and the card flips
`unbound → ready` on its own. `--iid 0` is correct for single-interface cards (RT3070); the
composite-device interface-id gotcha is [issue #206](https://github.com/pbatard/libwdi/issues/206),
deferred. Errors render through a `ModalScreen` (reuse the `ChannelFilterDialog` pattern =
the §2c hardware-failure UX).

**Revert ("Restore normal Wi-Fi") — no stored state required.** Binding to WinUSB does *not*
delete the stock driver: it stays in the Windows driver store and WinUSB just overrides the
active selection. Revert = `pnputil /delete-driver <oemNN.inf> /uninstall /force` +
`pnputil /scan-devices`, which re-points the device to the next-best match (the stock driver,
still in the store) and PnP rebinds it. The `oemNN.inf` is read **at runtime** off the bound
device (`DEVPKEY_Device_DriverInfPath`) — no pre-bind snapshot. After a clean revert the card
is back on its native driver, so libusb can't open it and it **correctly reappears as
present-but-unbound** in our own splash: a self-verifying round-trip (Install → ready →
Restore → ⚠). Zadig itself has no revert button ([libwdi #8](https://github.com/pbatard/libwdi/issues/8)),
so this is a genuine wifit3-over-Zadig win. Sole caveat: revert leaves a dead adapter only if
*no* fallback driver remains in the store (rare — inbox / Windows-Update drivers persist);
detect that post-rescan and surface it via the modal rather than silently bricking the card.

**Commit sequence.** (1) `setup/` scaffolding + VID:PID single-source from `_all_drivers()` +
vendored exe + `PROVENANCE.md`; (2) `install_winusb` wired to the button + re-refresh + error
modal; (3) runtime revert + in-TUI Restore button + the round-trip. The first *live* bind on
the RT3070 waits until (3) lands and is **user-greenlit** (it rebinds the card off `netr28ux`).

## Splash flow — revised to a select-time WinUSB check (2026-06)

Tier-0 as first shipped classified every card at **poll time** (`refresh()` → `_is_openable()`
→ open the device). Two problems surfaced on hardware (RTL8187L), and they reshape the flow:

- **The openability probe is a blocking libusb open**, run for *every* device on the Textual
  event loop *every ~1 s*. It froze the whole UI for ~1 s per poll — plus the startup paint
  and the quit. And on Windows WinUSB the probe's handle is **exclusive**: leaking it (we did
  — `get_active_configuration()` with no dispose) wedged the device and flipped a *ready* card
  to "unbound" on the next poll. The leak is fixed (dispose after probing), but the blocking
  open on the hot path is the deeper issue.
- It's also **worse UX**: a wall of "needs WinUSB / install Zadig" notices on a screen whose
  whole job is "pick a card."

**Revised flow — the WinUSB check moves off the poll and onto device *selection*:**

- **Poll = descriptor-only.** `find()` → match `SUPPORTED_IDS` by VID:PID → list. No open, no
  openability class, no "present-but-unbound" rows, no OS notice. Cheap enough to poll more
  often (near-instant hotplug). Driver construction is deferred too, so *every* supported
  VID:PID shows — including not-yet-ported placeholders, so the RT3070-placeholder gap (a card
  that can't construct used to vanish) dissolves.
- **A green START** button to the right of the (narrower) list; Enter also starts.
- **On START** (or Enter), **connect-first**: a WinUSB-bound card (the common case) just
  connects — no probe, no extra open, no lag. *Only if connect fails* do we run the
  (blocking, Windows-specific) openability probe; if the card isn't WinUSB-bound, a plain
  Yes/No modal offers a one-time install → Yes runs `install_winusb()`, re-finds the
  re-enumerated card, and connects; No returns to the list. The lazy probe-on-failure is what
  keeps the happy path open-free (the splash's original `is_openable`-at-select idea would
  have opened every started card twice).
- **Restore** ("Restore Wi-Fi driver") is **dropped from the splash** — there's no longer a
  poll-time "unbound" state to hang it off, and a lone orange button cluttered the screen.
  `restore_driver()` + the SetupAPI lookup stay in the backend, to resurface later behind a
  keybind or a post-install toggle.

Net: the splash is logo + list + START; the privileged WinUSB install becomes a deliberate,
explained, one-card action *at the moment the user commits to a card*. The install/revert
**mechanics** (wdi-simple, pnputil, SetupAPI) are unchanged — only *when* they fire. This
pivots the Tier-0 "classify at discovery" framing above to "list at discovery, check at
select"; the alpha gate (no silent failure) still holds — it just happens one beat later.

**Bugs fixed en route (on hardware):**
- **Handle leak** in `_is_openable()` — dispose after probing; a ready card no longer flips to
  unbound and the device no longer wedges until replug. (Tier-0 regression, commit 09cefd7.)
- **`install_winusb()` `--dest`** — wdi-simple's default extraction dir is the *relative*
  `usb_driver`, which an elevated (System32-CWD) process can't write → `WDI_ERROR_ACCESS (-3)`,
  the "Access denied" install error. Now pinned to an absolute `%TEMP%` dir.
- **Caveat observed:** removing WinUSB from a card that never had a native Windows driver
  leaves it driverless ("Other Devices") — correct behaviour, not a revert failure.

## Linux (kernel detach / udev)

Three levers, increasing bluntness:

1. **Runtime detach** — PyUSB `dev.detach_kernel_driver(intf)` /
   `set_auto_detach_kernel_driver(True)` (the `USBDEVFS_DISCONNECT` ioctl). Per
   session; the kernel re-attaches on replug, so **reversion on Linux is free.**
2. **udev permission rule** — `SUBSYSTEM=="usb", ATTRS{idVendor}=="..",
   ATTRS{idProduct}=="..", TAG+="uaccess"` (logind seat access) or `MODE="0660",
   GROUP="plugdev"`. Grants the user the device node.
3. **udev unbind-on-plug** — a `RUN+=` rule that unbinds the device from its kernel
   driver as it appears, so wifit3 finds it already free. Finicky and distro-variable
   (this is the part you were rightly unsure about).

- **Recommended path.** Ship one udev rules file covering all supported VID:PIDs
  (perms via `uaccess`), install it once via `pkexec`, and use PyUSB auto-detach at
  runtime. Fallback for headless/minimal boxes: `sudo wifit3` + a printed one-liner.
- **Don't** blanket-blacklist the kernel modules — that kills the card as normal
  Wi-Fi system-wide. Only on explicit opt-in.

### [VERIFY-L1] Does DISCONNECT need root, or just node write-access? ⚠ decides whether sudo ever disappears

If `USBDEVFS_DISCONNECT` keys off **write permission on the usbfs node** (my read)
rather than `CAP_SYS_ADMIN`, then lever 2 (a `uaccess`/`0660` udev rule) is enough
for *both* opening **and** detaching as a non-root user — install the rule once,
never sudo again. If it actually requires root, we fall back to lever 3 (unbind-on-
plug) or per-run sudo. **Test:** with a permissive udev rule installed and as a
normal user, call `detach_kernel_driver` on a kernel-claimed card.

### [VERIFY-L2] Which of our kernel modules resist clean detach

mac80211 Wi-Fi drivers sometimes hold the netdev and won't detach cleanly. Walk the
README's module list — `ath9k_htc`, `rtl8xxxu`, `mt76x2u`, `rt2800usb`, … — and note
per-module whether lever 1 works or it needs lever 3.

## Cross-cutting

- **One source of truth for VID:PIDs.** Drivers already declare `SUPPORTED_IDS`
  (`engine/protocols`, surfaced via `wlan/manager.py:_all_drivers`). Generate the
  udev rules file *and* the Windows VID:PID list **from that registry** — never
  hand-maintain a parallel list. A `wifit3 --setup` / `--emit-udev` entry point reads
  it and does the right thing per-OS.
- **Integration points.** Discovery is `WlanDeviceManager.refresh()`
  (`src/wifit3/wlan/manager.py`) — now descriptor-only (VID:PID match, no open). The
  openability check (`_is_openable`, which *does* open) moved off the poll and onto device
  selection — see "Splash flow" above. The UX surface is `src/wifit3/ui/screens/splash.py`.
- **Reuse the §2c error path.** A failed WinUSB install renders through the Hardware-failure
  UX modal + Details box (FEATURES.md § Hardware-failure UX) — `SetupErrorDialog`, already
  built.

## Open questions / must-verify (the honest list)

- [x] **W1** — does `libusb_package` enumerate unbound devices on Windows? **YES**
  (verified RT3070/`netr28ux`, never-Zadig'd): `find()` enumerates it; the *open*
  step raises `NotImplementedError` (libusb `NOT_SUPPORTED`). No parallel
  enumeration needed — classify on found-but-can't-open. See [VERIFY-W1] above.
- [ ] **L1** — `USBDEVFS_DISCONNECT` as non-root with a permissive udev rule? (decides
  whether per-run sudo ever goes away.)
- [ ] **W2** — SmartScreen / AV-EDR friction invoking our *unsigned* `wdi-simple.exe`
  elevated. Maintainer confirms the unsigned-elevated risk is ours to own; libwdi self-signs
  the driver *cat* at install (FAQ) so the driver itself lands clean. To be settled
  empirically on the RT3070 once commit (2) lands.
- [ ] **L2** — per-module detach-vs-unbind for our kernel drivers.
- [ ] `pkexec` availability across target distros (Kali/Ubuntu yes; minimal/headless
  → sudo fallback).
- [ ] arm64 Windows: libwdi's VS2022 workflow builds x64/Win32 only — no arm64 `wdi-simple.exe`
  yet. (arm Linux libwdi availability also open.)

## Recommendation

- **Alpha gate: Tier 0 only.** Detect-and-guide is cheap, elevation-free, and removes
  most support pain. Do not bet the alpha on the hard automation.
- **Tier 1 Windows (libwdi)** is the highest-value automation *and* the riskiest
  bundling — prototype + test on real Win10/11 (settle W1/W2) before committing.
- **Tier 1 Linux** is mostly "ship a udev file + `pkexec` installer + auto-detach" —
  lower risk; do it alongside, gated on L1.
- **Tier 2** (in-TUI wizard + revert) post-alpha.

This reaches §2d's "no manual Zadig, no terminal sudo" goal **incrementally**, with
the alpha resting on the part that always works.
