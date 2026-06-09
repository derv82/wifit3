# Wifit3 — Linux Device Setup (kernel detach / udev) plan

How a Linux user goes from "plugged the card in" to "wifit3 sees it," without a `sudo rmmod`
incantation. The **Windows half of device-setup is shipped** (detect → install WinUSB →
revert, all in-TUI); this is the remaining **Linux** half. Design for RELEASE-PLAN § 2d.

## The constraint

wifit3 talks to cards through libusb, so the card must be reachable by libusb — on Linux the
**kernel driver must release** the device (detach / unbind / rmmod). That's a **privileged,
one-time-ish action**: we can't delete it, only (a) reduce it to one clean prompt, (b) make it
self-explaining, and (c) make it reversible. The honest goal is *at most one `pkexec` prompt
ever, ideally zero per-run sudo* — not "fully magic," which would be a lie.

Second constraint, easy to forget: **detaching the card stops it being a normal Wi-Fi
adapter.** That must be visible and reversible — and on Linux reversion is essentially
**free**: the kernel re-attaches the driver on replug, and a per-session
`detach_kernel_driver` doesn't persist. (No "Restore" action to build — a replug *is* the
undo.)

## Where it hooks into the app

The splash already carries the whole UX shell, and it's **platform-generic**: descriptor-only
discovery → a list + a green START button → **connect-first** (try to open the card; only *on
failure* run the privileged-setup path) → a Yes/No confirm modal → an error modal
(`SetupErrorDialog`). Linux only needs to fill in *its half of the privileged action* on the
connect-failure path. Everything above it (picker, connect attempt, modals, re-find-and-
connect) is built.

Concretely: when `connect()` fails because the device is kernel-claimed (can't be opened),
that's the Linux equivalent of "needs setup" — offer the fix (detach now, optionally install
the udev rule so it's automatic next time), then retry connect.

## Three levers (increasing bluntness)

1. **Runtime detach** — PyUSB `dev.detach_kernel_driver(intf)` /
   `set_auto_detach_kernel_driver(True)` (the `USBDEVFS_DISCONNECT` ioctl). Per session; the
   kernel re-attaches on replug, so **reversion is free.**
2. **udev permission rule** — `SUBSYSTEM=="usb", ATTRS{idVendor}=="..",
   ATTRS{idProduct}=="..", TAG+="uaccess"` (logind seat access) or `MODE="0660",
   GROUP="plugdev"`. Grants the user the device node.
3. **udev unbind-on-plug** — a `RUN+=` rule that unbinds the device from its kernel driver as
   it appears, so wifit3 finds it already free. Finicky and distro-variable (the part to be
   most cautious about).

- **Recommended path.** Ship one udev rules file covering all supported VID:PIDs (perms via
  `uaccess`), install it once via `pkexec`, and use PyUSB auto-detach at runtime. Fallback for
  headless/minimal boxes: `sudo wifit3` + a printed one-liner.
- **Don't** blanket-blacklist the kernel modules — that kills the card as normal Wi-Fi
  system-wide. Only on explicit opt-in.

### [VERIFY-L1] Does DISCONNECT need root, or just node write-access? ⚠ decides whether sudo ever disappears

If `USBDEVFS_DISCONNECT` keys off **write permission on the usbfs node** (my read) rather than
`CAP_SYS_ADMIN`, then lever 2 (a `uaccess`/`0660` udev rule) is enough for *both* opening
**and** detaching as a non-root user — install the rule once, never sudo again. If it actually
requires root, fall back to lever 3 (unbind-on-plug) or per-run sudo. **Test:** with a
permissive udev rule installed and as a normal user, call `detach_kernel_driver` on a
kernel-claimed card.

### [VERIFY-L2] Which of our kernel modules resist clean detach

mac80211 Wi-Fi drivers sometimes hold the netdev and won't detach cleanly. Walk the README's
module list — `ath9k_htc`, `rtl8xxxu`, `mt76x2u`, `rt2800usb`, … — and note per-module whether
lever 1 (runtime detach) works or it needs lever 3 (unbind-on-plug).

## Cross-cutting

- **One source of truth for VID:PIDs.** Drivers declare `SUPPORTED_IDS` (`engine/protocols`,
  surfaced via `wlan/manager.py:_all_drivers`); `setup.ids_from_registry()` already flattens
  them to a deduped list. Generate the udev rules file **from that registry** — never a
  hand-maintained parallel list. A `wifit3 --setup` / `--emit-udev` entry point reads it and
  writes the rules.
- **Reuse the error modal.** A failed detach / setup renders through `SetupErrorDialog`
  (already built) — the same path the rest of the setup flow uses.

## Open questions / must-verify

- [ ] **L1** — `USBDEVFS_DISCONNECT` as non-root with a permissive udev rule? (Decides whether
  per-run sudo ever goes away — the whole ballgame.)
- [ ] **L2** — per-module detach-vs-unbind for our kernel drivers.
- [ ] `pkexec` availability across target distros (Kali / Ubuntu desktop: yes; minimal /
  headless: → `sudo` fallback + printed one-liner).
- [ ] arm Linux (Raspberry Pi, Kali-arm): confirm PyUSB detach + the udev install behave the
  same (no reason they shouldn't, but untested).

## Recommendation

Linux setup is the **lower-risk** half — "ship a udev file (generated from `SUPPORTED_IDS`) +
a one-time `pkexec` installer + PyUSB auto-detach at runtime" — and it slots into the
already-built splash flow on the connect-failure path. Gate the design on **L1**: if a
permissive udev rule alone lets a non-root user *detach* (not just open), then sudo disappears
entirely after the single rule install — the best outcome. Best built from a real Linux box
(Kali) where L1/L2 can be measured directly rather than guessed.

Target: a card on Linux works with **at most one** `pkexec` prompt ever, and ideally zero
per-run sudo.
