# Wifit3 — Linux Device Setup (kernel detach / udev) plan

How a Linux user goes from "plugged the card in" to "wifit3 sees it," without a `sudo rmmod`
incantation. The **Windows half of device-setup is shipped** (detect → install WinUSB →
revert, all in-TUI); this is the remaining **Linux** half. Design for RELEASE-PLAN § 2d.

## Verified results — 2026-06-09 (Kali 6.18.12, normal user uid 1000)

Ran `scripts/linux_setup/probe_l1_l2.py` across the full card fleet (17 plugs,
13 distinct VID:PIDs) with the permissive udev rule installed. **Both gating
questions resolved — the best-case outcome holds.** Transcript + TSV:
`usb_dumps_new/linux-setup-results/`.

### L1 — RESOLVED: holds. Per-run sudo disappears after one rule install.

With the udev rule giving the usbfs node `crw-rw---- root:plugdev` (writable
by uid 1000), a **non-root** `detach_kernel_driver` succeeded on *every* Wi-Fi
module. The control case proves the mechanism: an earlier run *before* the rule
applied (node `root:root`) failed every card with `errno 13 (Access denied)` on
**open** — the same permission that gates detach. So `USBDEVFS_DISCONNECT` keys
off **node write-access, not `CAP_SYS_ADMIN`** ([VERIFY-L1] confirmed): the one
rule install grants open *and* detach, and per-run sudo is gone for good.

### L2 — RESOLVED: every module detaches via lever 1. Lever 3 not needed.

Runtime detach (lever 1) cleanly released all of:

  ath9k_htc · rtl8187 · rtl88x2bu · mt76x2u · rtl8814au · mt76x0u ·
  rtw88_8812au · 8188eu · mt7921u (composite) · rt2800usb
  (RT5572/RT3070/RT5372) · rt2500usb · rtl8821au

**No module required lever 3 (unbind-on-plug).** The finicky, distro-variable
`RUN+=` rule drops out of the plan — reach for it only if some future module
resists, which none here did.

### New finding — composite BT+Wi-Fi combos (MT7921AU, 0e8d:7961)

The AXML enumerates **4 interfaces**; the kernel binds `btusb` (if0/if1) +
`mt7921u` (if3). Two consequences for the Linux connect path:

1. **Detach must loop every interface**, not just intf 0. The probe did
   `if0 DETACHED · if1 none · if2 none · if3 DETACHED`. Our drivers' `_claim`
   detaches interface 0 only (e.g. `rt2800usb/driver.py:_claim`) — fine for
   single-interface cards, but the combo needs an all-interface loop.
2. **udev-vs-firmware-reenum perms race.** On some plugs the node came up
   `crw------- root:root` (rule lost the race → INCONCLUSIVE); on others
   `crw-rw---- root:plugdev` → success. MT7921 loads firmware and
   re-enumerates, so the rule sometimes doesn't re-apply to the post-FW node.
   A **replug** (clean cold-plug) resolved it every time. The connect-failure
   path should surface "replug" guidance when the node is root-owned, the same
   spirit as [[feedback_warm_reattach]]'s wedged-pipe message.

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
   most cautious about). **Verified unnecessary (2026-06-09): every module detached via lever 1.**

- **Recommended path.** Ship one udev rules file covering all supported VID:PIDs (perms via
  `uaccess`), install it once via `pkexec`, and use PyUSB auto-detach at runtime. Fallback for
  headless/minimal boxes: `sudo wifit3` + a printed one-liner.
- **Don't** blanket-blacklist the kernel modules — that kills the card as normal Wi-Fi
  system-wide. Only on explicit opt-in.

### [VERIFY-L1] Does DISCONNECT need root, or just node write-access? — ✅ RESOLVED: node write-access (see Verified results)

If `USBDEVFS_DISCONNECT` keys off **write permission on the usbfs node** (my read) rather than
`CAP_SYS_ADMIN`, then lever 2 (a `uaccess`/`0660` udev rule) is enough for *both* opening
**and** detaching as a non-root user — install the rule once, never sudo again. If it actually
requires root, fall back to lever 3 (unbind-on-plug) or per-run sudo. **Test:** with a
permissive udev rule installed and as a normal user, call `detach_kernel_driver` on a
kernel-claimed card.

### [VERIFY-L2] Which of our kernel modules resist clean detach — ✅ RESOLVED: none (all detach via lever 1)

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

- [x] **L1** — `USBDEVFS_DISCONNECT` as non-root with a permissive udev rule? **Yes** — keys off
  node write-access, not root. Per-run sudo gone after one rule install (2026-06-09).
- [x] **L2** — per-module detach-vs-unbind for our kernel drivers. **All detach via lever 1**;
  none need unbind-on-plug (2026-06-09).
- [ ] `pkexec` availability across target distros (Kali / Ubuntu desktop: yes; minimal /
  headless: → `sudo` fallback + printed one-liner).
- [ ] arm Linux (Raspberry Pi, Kali-arm): confirm PyUSB detach + the udev install behave the
  same (no reason they shouldn't, but untested).

## Recommendation

Linux setup is the **lower-risk** half — "ship a udev file (generated from `SUPPORTED_IDS`) +
a one-time `pkexec` installer + PyUSB auto-detach at runtime" — and it slots into the
already-built splash flow on the connect-failure path. **L1 is confirmed** (2026-06-09): a
permissive udev rule alone lets a non-root user *detach*, not just open, so sudo disappears
entirely after the single rule install — the best outcome, now measured rather than guessed.
Lever 3 (unbind-on-plug) is **not needed** — every module detached via lever 1. The one
implementation caveat is composite BT+Wi-Fi combos (MT7921AU): detach **all** interfaces, and
fall back to a "replug" prompt when the post-firmware node re-enumerates root-owned.

Target — now achievable: a card on Linux works with **at most one** `pkexec` prompt ever (the
rule install) and **zero** per-run sudo.

## Implementation plan (parked 2026-06-11)

Full plan saved at `~/.claude/plans/precious-tickling-canyon.md` (Claude Code plan
**precious-tickling-canyon**). **Implemented 2026-06-12 — see "Landed" below.**

One refinement to the Recommendation above, decided while planning: ship a **per-device**
permission rule scoped to the single VID:PID the user activates — *not* one blanket file across
all supported models — so a second supported card kept as the normal internet adapter stays
untouched (mirrors the Windows "bind only the card you picked" UX). The blanket file survives as
an explicit `wifit3 --emit-udev` power-user opt-in. The no-install bypass is `sudo wifit3`; a
one-time per-device rule is the "never sudo again for this card" path. (A permission rule never
unbinds the driver or hides the card — it only grants node access; the card stays a normal Wi-Fi
adapter until wifit3 detaches it at runtime.)

Shape: new `setup/linux.py` (`free_device()` mirroring `setup/windows.py:install_winusb`,
helpers lifted from `scripts/linux_setup/probe_l1_l2.py`) + a Linux branch on the
connect-failure path in `ui/screens/splash.py:perform_start` + a parametrized `ConfirmInstallDialog`.

## Landed — 2026-06-12

Shipped exactly the parked shape, plus the **uninstall (✕) button** (both OSes) that the plan
flagged as a logical companion:

- **`setup/linux.py`** — `free_device(vid, pid, desc)` (per-device rule install, one
  `pkexec`→`sudo`→manual prompt), `remove_rule(vid, pid)` (uninstall; missing rule = benign
  no-op), `build_rule_text()` (carries the trailing-comment fix — each description on its own
  `#` line; a regression test asserts no rule line contains `#`), `emit_udev_text()` (blanket
  file from the live registry), wired to `wifit3 --emit-udev`.
- **`wlan/manager.py:linux_needs_permission()`** — usbfs-node-writability stat. Needed because a
  kernel-claimed Linux card still *passes* `is_openable` (descriptors read; the claim fails
  later), so node write-access — not openability — is the "install the access rule" signal.
- **`ui/screens/splash.py`** — Linux branch on the connect-failure path (probe → confirm →
  `free_device` → re-find → retry, with a replug hint on retry failure for FW-reenum combos);
  the **✕ uninstall button** next to START (tooltip-labelled) → `restore_driver` (Windows,
  already built) / `remove_rule` (Linux) via a new `ConfirmUninstallDialog`. `ConfirmInstallDialog`
  parametrized (defaults unchanged) so the Linux prompt reuses it with "Device Access" wording.
- **18 unit tests** (`tests/setup/test_linux.py`) — rule text, path naming, `run_privileged`
  argv, install/remove classification, the no-trailing-comment regression. Windows-runnable.

**Behaviour to test on Kali (the live path can't run on Windows):**

- **First card, user (non-root):** START → `pkexec` graphical *Enter-Password* prompt → per-VID:PID
  rule written → `udevadm trigger` → node user-writable → retry connect succeeds non-root. (Confirm
  the polkit prompt actually pops — earlier probe runs never surfaced it.)
- **Same card, later sessions:** no prompt (rule persists).
- **A *different* card:** prompts **once more** — the rule is per-VID:PID by design. This is the
  intended "second supported card kept as the normal adapter stays untouched" behaviour, **not**
  a bug. ("Install once, every supported card free" is the blanket `--emit-udev` opt-in.)
- **`sudo wifit3`:** no rule needed at all (root opens + detaches everything) — the zero-install
  bypass.
- **Uninstall (✕):** Windows removes the WinUSB binding immediately; Linux removes the rule (card
  returns to its kernel driver on the next replug).

Still open (unchanged): `pkexec` availability on minimal/headless distros (→ `sudo`/manual), and
arm-Linux parity.
