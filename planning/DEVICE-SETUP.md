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
a gate.**

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
  (`src/wifit3/wlan/manager.py`); the Tier-0 classifier slots between `usb.core.find`
  and `from_usb_device` (catch the open/claim failure → classify). The UX surface is
  `src/wifit3/ui/screens/splash.py`.
- **Reuse the §2c error path.** The "present-but-unbound" state should render through
  the Hardware-failure UX modal + Details box (FEATURES.md § Hardware-failure UX),
  not a bespoke widget.

## Open questions / must-verify (the honest list)

- [x] **W1** — does `libusb_package` enumerate unbound devices on Windows? **YES**
  (verified RT3070/`netr28ux`, never-Zadig'd): `find()` enumerates it; the *open*
  step raises `NotImplementedError` (libusb `NOT_SUPPORTED`). No parallel
  enumeration needed — classify on found-but-can't-open. See [VERIFY-W1] above.
- [ ] **L1** — `USBDEVFS_DISCONNECT` as non-root with a permissive udev rule? (decides
  whether per-run sudo ever goes away.)
- [ ] **W2** — libwdi catalog signing / SmartScreen friction on current Win10/11.
- [ ] **L2** — per-module detach-vs-unbind for our kernel drivers.
- [ ] `pkexec` availability across target distros (Kali/Ubuntu yes; minimal/headless
  → sudo fallback).
- [ ] arm64 Windows / arm Linux libwdi binary availability.

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
