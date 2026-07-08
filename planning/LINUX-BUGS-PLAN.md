# Plan — Fix the Linux-specific bugs

Scope: the two genuinely **Linux-platform** bugs in `BUGS.md` (kernel-driver binding / install / uninstall).
The per-card entries and the other cross-cutting items are cross-platform (port/UI) bugs, not Linux
integration bugs, so they're out of scope here. The two in scope:

- **Bug A — Foreign-warmed chip → degraded RX until replug** (release blocker) — `BUGS.md` "Cross-cutting".
- **Bug B — Linux uninstall leaves a shared kernel driver blacklisted** — `BUGS.md` "Cross-cutting".

> **Reality check first.** `BUGS.md` is stale relative to the code. The replug mechanism and live
> per-chipset module discovery already exist (`LINUX_REPLUG_AFTER_MODPROBE`, `setup/linux.py`
> `discover_kernel_modules`). Neither bug needs a green-field build — both are **correctness gaps in
> a mechanism that's ~80% there**. That shrinks the work and changes where the risk is.

The relevant code, all confirmed by reading:
- `src/wifit3/setup/linux.py` — module discovery, blacklist/udev emitters, `install_rule`/`remove_rule`.
- `src/wifit3/setup/__init__.py` — `SetupTarget`, `target_for_vidpid`.
- `src/wifit3/ui/screens/splash.py` — `perform_start` (Linux branch L291-391), `perform_uninstall` (L409-495).
- `src/wifit3/engine/protocols.py` — `CONFLICTING_LINUX_MODULES`, `LINUX_REPLUG_AFTER_MODPROBE`.
- `src/wifit3/wlan/manager.py` — `linux_kernel_driver_bound`, `linux_needs_permission`, `linux_wait_for_access`, `get_interface_by_vidpid`, `refresh`.
- `tests/setup/test_linux.py` — existing coverage (no shared-module or replug-flow tests yet).

---

## Bug A — Foreign-warmed chip → degraded RX until replug

### Root cause
A card the **kernel** driver already warmed (firmware uploaded, PHY/AGC in the kernel's config) can't be
cold-reset from userland on most Realtek/Ralink silicon. `modprobe -r` unbinds but doesn't power-cycle,
so wifit3 takes over a half-configured chip → silently degraded RX (BUGS.md: RTL8814AU 5 bcn/20 s vs 106
after replug). Only a physical replug (real power cycle → cold boot) recovers it.

Three self-cold chips are exempt because they *do* force a cold-equivalent state in userland:
- **AR9271** — re-enumerates on firmware download (`ar9271_v2` `_await_reenumeration`).
- **mt76x0u** — `modprobe -r` cold-re-enumerates the card (`MT76X0U.md:49`).
- **mt76x2u** — `power.force_power_cycle` clears the WLAN block to cold-equivalent (`MT76X2U.md:76`).

### What already exists
- `LINUX_REPLUG_AFTER_MODPROBE` flag on the driver protocol (default `False`).
- Splash honors it: after `install_rule`, if `target.replug_after_modprobe` it shows a **passive** status
  line ("Please Unplug, Replug, then press START") and returns to the picker instead of auto-connecting
  (`splash.py:341-351`).
- Only **`mt7921au`** sets the flag today.

### The two gaps (this is the actual bug)
1. **Classification gap.** The RT/Realtek drivers that can't self-cold do **not** set the flag, so after an
   install they auto-connect warm → degraded RX. (This is the RTL8814AU symptom in BUGS.md.)
2. **Happy-path hole (not in BUGS.md, found while planning).** `perform_start` tries `self._connect(iface)`
   *before* any taint check (`splash.py:236`). A foreign-warm card whose node is already writable — running
   as root, or the node made writable by a prior install, or the module left resident by a sibling (Bug B) —
   warm-reattaches straight to the scanner. The warm-reattach RX smoke test only fails on *fully dead* RX, not
   *degraded* RX, so it sails through. Setting the flag alone does **not** close this; it only guards the
   install branch.

### Fix (three parts)

**A1 — Flip the default to "replug required"; opt the self-colders out. (DECIDED with Lead.)**
Keep the flag name `LINUX_REPLUG_AFTER_MODPROBE`; make its **default `True`**. `WlanDriver` is a
`typing.Protocol` and no driver subclasses it (verified — every driver is a bare `class XDriver:`), so the
Protocol's `= False` is never inherited and the effective default is set entirely by the one `getattr` call.
Concretely:
- `setup/__init__.py:47` — change the `getattr(driver_cls, "LINUX_REPLUG_AFTER_MODPROBE", False)` default to
  `True`.
- **Set `LINUX_REPLUG_AFTER_MODPROBE = False` explicitly** on the three self-colders (`ar9271_v2`, `mt76x0u`,
  `mt76x2u`). They rely on the attr being *absent* today; once absent means "replug required," they must opt
  out or they'd wrongly force a replug (regressing their working auto-connect). **This is the load-bearing
  step — the default flip is wrong without it.**
- `protocols.py:84` — update the declared default to `= True` and the docstring, so the documented default
  matches the effective one (cosmetic — nothing reads the value — but leaving it `False` is a footgun).
- `mt7921au`'s explicit `= True` becomes redundant but harmless; leave it (self-documents intent).

The table below is now just the HW-confirmation checklist (default = replug; only the top three opt out).

Classification (derived from chip docs + warm-reattach "can't reset in userland" messages; ⚠ = confirm on HW):

| Driver | Can self-cold in userland? | Replug required? | Basis |
|---|---|---|---|
| ar9271_v2 | yes (FW re-enum) | no | `_await_reenumeration` |
| mt76x0u | yes (`modprobe -r` re-enum) | no | MT76X0U.md:49 |
| mt76x2u | yes (`force_power_cycle`) | no | MT76X2U.md:76 |
| mt7921au | no | **yes** (already set) | MT7921AU.md:110 |
| rtl8187 | no | **yes** ⚠ | warm bits persist, AGC dead |
| rtl8188eus / _dkms | no | **yes** ⚠ | warm-reattach "please replug" msg |
| rtl8812au / _dkms | no | **yes** ⚠ | " |
| rtl8814au_dkms | no | **yes** ⚠ | BUGS.md symptom source |
| rtl8821au / _dkms | no | **yes** ⚠ | `mac.py:236` "RX dead until re-enum" |
| rtl8821cu_dkms | no | **yes** ⚠ | " |
| rtl8822bu / _dkms | no | **yes** ⚠ | " |
| rtw88_8814au | no | **yes** ⚠ | " |
| rt2500usb, rt2800usb, rt3070, rt5370, rt5372 | no ⚠ | **yes** ⚠ | BUGS.md "RT* must be replugged"; no FW-reenum path |

**A2 — Close the happy-path hole.** In `perform_start`, on Linux, before the happy-path `_connect`, if the
card is foreign-warm (`device_manager.linux_kernel_driver_bound(iface)` is `True`) and the driver is *not*
self-cold, skip the silent warm connect and route into the setup/replug flow. `linux_kernel_driver_bound`
is exactly the discriminator: it's `True` for a kernel-warmed card and `False` for a wifit3-warmed card
(kernel blacklisted → not bound), so a prior-wifit3-session warm reattach still takes the fast path.

**A3 — Upgrade the passive message to an active replug-detection modal.** Replace the status-text branch
(`splash.py:341-351`) with a modal that:
1. shows "Unplug the card…" and polls the bus for the target VID:PID to **disappear**;
2. shows "Now replug it…" and polls for it to **reappear** (new bus address → guaranteed cold);
3. pops and auto-connects the now-cold card (or falls back to the picker via `reset_for_reentry`).

Build the poll from existing primitives — `device_manager.refresh()` + `get_interface_by_vidpid(vid,pid)`,
mirroring `linux_wait_for_access` (predicate = "present/absent" instead of "writable") and AR9271's
`_await_reenumeration`. `refresh()`'s dedup signature already includes bus address (`manager.py:177`), so a
replug is observable. Modal template: a self-polling `ModalScreen` like `ConfirmInstallDialog`'s
`set_interval`, or the `PropagatingDialog` push→await→pop idiom (`splash.py:358-363`). Include a "Skip /
connect anyway" escape for power users, and a timeout → fall back to the picker.

New helper to add: `WlanDeviceManager.linux_wait_for_presence(iface, *, present: bool, timeout)` — the poll
loop A3 needs, symmetric with `linux_wait_for_access`.

### Tests (A)
- Unit: `target_for_vidpid` maps each driver to the correct self-cold/replug flag (table above).
- Unit: `linux_wait_for_presence` returns on disappear then reappear; times out cleanly (mock `refresh`).
- Splash logic: foreign-warm (`linux_kernel_driver_bound=True`) + non-self-cold → replug modal, **not** the
  happy-path connect; wifit3-warm (`bound=False`) → fast path. Mock the manager, assert the branch taken.
- Regression: self-cold chips still auto-connect (no modal).

### HW verification (A) — agent-runnable, passive only
For each non-self-cold card: kernel-warm it (let the kernel bind), run wifit3, install, confirm the replug
modal appears; replug; confirm cold RX (beacon-rate bar vs the known-good reference AP, per the beacon-rate
methodology) matches the post-replug baseline in that card's `<CHIP>.md`. Resolve every ⚠ in the table. No
live TX involved — this is RX/replug only, inside the agent's autonomy.

### Risks (A)
- Flag inversion touches every driver file (mechanical) + the `SetupTarget` plumbing + one test. Low risk,
  wide diff — do it as its own commit.
- Modal + poll interacts with the 0.5 s discovery timer already running on splash; pause it during the modal
  (the `perform_start` worker already pauses `_refresh_timer`).
- A card that enumerates to the same bus address on replug would defeat "new address = cold"; fall back to
  "disappeared then reappeared at all" as the cold signal, which is what actually matters.

---

## Bug B — Linux uninstall leaves a shared kernel driver blacklisted

### Root cause
The blacklist `.conf` is named per **wifit3 registry key** (`/etc/modprobe.d/wifit3-<key>.conf`), but the
line inside it blacklists the **kernel module** discovered live. Several distinct keys map to one module:
`rt5372`, `rt5370`, `rt3070`, and `rt2800usb` (which itself claims RT5572/RT3572) **all resolve to
`rt2800usb.ko`**. Consequences:
- Install key A writes `wifit3-A.conf` with `blacklist rt2800usb`; install key B writes `wifit3-B.conf` with
  the same line. Uninstalling A removes only `wifit3-A.conf` → B's file keeps `rt2800usb` blacklisted → A
  (and the whole family) never rebinds the kernel driver.
- Worse false-negative: a sibling (e.g. RT5572, key `rt2800usb`) that was **never explicitly installed** —
  it only showed up unbound because A's blacklist displaced it — has no `.conf` of its own. Uninstalling it
  hits `remove_rule`'s "No udev rule installed — nothing to remove" (`linux.py:349-352`) while A's file
  silently keeps it blocked. The user has no way to discover why their card won't work normally again.

This is a **granularity mismatch**: the user thinks per-card; the blacklist is per-kernel-module; the mapping
is many-to-one. Handing over one card *necessarily* displaces its whole module family — that's inherent — but
today it's neither correct on uninstall nor legible to the user.

### Design decision (DECIDED with Lead): keep per-chipset files; let modprobe's union do the refcount.
File naming/content stays as today:
- **udev rule** `60-wifit3-<key>.rules` = VID:PID-specific access grants (no modules). Per-chipset, correct.
- **modprobe blacklist** `wifit3-<key>.conf` = module-specific `blacklist <mod>` / `install <mod> /bin/true`
  (no VID:PIDs). Named by chipset key.

The insight that makes this correct with no restructure: `modprobe` reads **all** `/etc/modprobe.d/*.conf`
and unions their `blacklist` directives, so **the set of files is the reference count.** Deleting one
chipset's `.conf` decrements automatically; the module stays blacklisted iff another file still lists it; it's
lifted only when the last one is removed. We therefore **never compute "what to unblacklist"** — that was the
feared hard problem, and it's a non-problem here. (A module-*named* file, `wifit3-rt2800usb.conf`, is the
intuitive-but-harder path: it's shared, so you'd hand-maintain a dependent-refcount comment inside it instead
of getting the union for free. Rejected for that reason.)

Module discovery stays live (`_bound_modules` ∪ `_resolve_via_modalias`), so DKMS vs mainline module names are
never hardcoded. modalias discovery works even on an *unbound* card (reads the alias table, not the binding),
so it's usable at uninstall too. Chipset→module is persisted in the `.conf` content; module→chipsets is a scan
of `wifit3-*.conf`. The files are the database — no separate ledger, no migration.

**Hard invariant (Lead):** every scan and every delete touches **only** wifit3's own files — the
`60-wifit3-*.rules` / `wifit3-*.conf` glob. We never read, rewrite, or delete a non-wifit3 modprobe/udev file,
even one that blacklists the same module.

**On orphaned files after the app is deleted (Lead's real worry):** we keep per-chipset filenames, so the
module name isn't in the filename — but the file *content* is self-documenting: the filename is `wifit3-*`
(greppable), and the body already carries `blacklist <module>` plus a "Delete this file (uninstall) to return
the card to normal" comment (`emit_blacklist_text`). A user spelunking `/etc/modprobe.d` after deleting wifit3
finds a clearly-labeled, self-explaining file. Deemed sufficient; no install-time family-displacement messaging
(cut — module names are 1–N and arbitrary, can't be shown cleanly, and ~nobody owns siblings).

### Fix

**B1 — Correct, reporting uninstall.** For card X: get X's module(s) (read X's own `wifit3-<key>.conf`, else
modalias-discover), delete X's udev rule + its `.conf`, then scan the *remaining* `wifit3-*.conf`. For each
module X needed, if any remaining file still blacklists it → the module stays blocked (a sibling is still
handed over, which is correct) and we **report** it: "`rt2800usb` is still blocked because chipset **K** is
handed to wifit3; uninstall it too to return this card to normal" (optionally offer a one-tap uninstall of K).
New `setup/linux.py` helpers: `modules_in_conf(path)` (parse `blacklist` lines) and
`confs_blacklisting(module) -> list[key]` (scan). Wire the report into `perform_uninstall`.

**B3 — Fix the "nothing to remove" false-negative.** When the user uninstalls a card that has **no `.conf` of
its own** but whose kernel module is blacklisted by a *sibling's* `.conf` (the never-explicitly-installed
PAU09 case), `remove_rule` today returns "nothing to remove." Instead: modalias-discover the card's module,
run `confs_blacklisting(module)`; the sibling blockers surface as the wide-uninstall targets in B2. This is
what makes that case recoverable at all.

**B2 — Two-radius uninstall (the UX win the Lead asked for).** In `perform_uninstall`, after computing X's
modules and the sibling set (via `confs_blacklisting`, wifit3 files only):
- **No siblings** → today's single "Uninstall" button; removes X's `.rules` + `.conf`.
- **Siblings found** → the confirm dialog shows **two** buttons, both "uninstall wifit3's rules," differing
  only in radius:
  - **narrow** — "Uninstall <X>": removes only X's two files. On a shared module this leaves the card
    displaced (a sibling still blacklists it) → the result message says so plainly (not a silent no-op).
  - **wide** — "Uninstall <X> + N related": removes X's files **and each direct sibling's `.rules` + `.conf`**,
    fully un-handing the family so the shared module is freed and every card returns to the kernel on replug.
- Siblings are **direct only** (share ≥1 module with X), non-transitive. Both buttons touch only `wifit3-*`
  files. `ConfirmUninstallDialog` grows an optional siblings arg (count + names) that adds the second button
  and the explanatory line; single-card uninstall is unchanged.

### Tests (B)
- Install two keys sharing one module → two `wifit3-<key>.conf` files, both blacklisting `rt2800usb`.
- Uninstall one of the two → its file is gone, the sibling's remains, and the result message says the module
  **stays** blacklisted because chipset K still holds it.
- Uninstall the last one → no `wifit3-*.conf` blacklists the module anymore (fully lifted).
- False-negative: uninstall a card with no `.conf` of its own whose module is blocked by a sibling's file →
  detect + name the culprit (not "nothing to remove").
- `modules_in_conf` / `confs_blacklisting` unit tests (parse + scan) against fixture files.
- `_safe(key)` still sanitizes the key into the `.conf` / `.rules` paths (unchanged).

### HW verification (B) — agent-runnable
Two Ralink cards that share `rt2800usb` (e.g. RT5372 + a PAU09/RT5572). Install RT5372, confirm PAU09 is
displaced; install PAU09; uninstall RT5372 → PAU09 stays displaced with a clear message; uninstall PAU09 →
`rt2800usb` unblacklisted, both return to the kernel on replug. (Requires the two cards; no TX.)

### Risks (B)
- No file-scheme change and no migration — the disk layout is identical to today, only uninstall's logic and
  messaging change. Lowest-risk of the two bugs.
- Reading a card's modules at uninstall can come up empty if the card is both unbound *and* unplugged and has
  no `.conf`; in that degenerate case fall back to `confs_blacklisting` over the driver's known modules /
  `CONFLICTING_LINUX_MODULES` hint, and never crash uninstall.

---

## Interaction between A and B
They share a cause. The most common way a card becomes **foreign-warm (Bug A)** is a sibling keeping the shared
`rt2800usb` module **resident (Bug B)**: the blacklist stops future *loads* but a resident module still binds a
freshly plugged device and uploads firmware. So fixing B (family-aware messaging + a correct, reporting
uninstall) *reduces* Bug A's incidence and its confusion, and Bug A's replug modal is the safety net for
whatever still slips through. Sequence B's uninstall fix to land before/with A's classification so the HW
verification of A isn't confounded by a resident sibling module.

---

## Sequencing / milestones (one commit each)  — code complete, HW sweep pending
1. ✅ **B1 + B3** (`5819139`) — uninstall backend: `plan_uninstall`, `modules_in_conf`,
   `confs_blacklisting`, `remove_rule(also_keys=)`, residual reporting. 9 unit tests.
2. ✅ **B2** (`a9e0a93`) — two-radius uninstall UI: `ConfirmUninstallDialog` narrow/wide buttons + sibling
   copy; `perform_uninstall` computes the plan and passes `also_keys`. 5 pilot tests.
3. ✅ **A1** (`2acfbfe`) — `LINUX_REPLUG_AFTER_MODPROBE` default flipped to `True`; explicit `= False` on
   `ar9271`/`ar9271_v2`/`mt76x0u`/`mt76x2u`; protocols.py doc. 3 classification tests.
4. ✅ **A2** (`d32ba73`) — happy-path hole closed: skip the fast connect for a foreign-warm, replug-required
   card (gated on `linux_kernel_driver_bound`).
5. ✅ **A3** (`98c6656`) — automatic replug modal (`ReplugModal` + `linux_wait_for_presence`), Skip escape,
   reuses the post-install auto-connect tail. 5 tests.
6. ⏳ **HW sweep (needs the user + hardware)** — resolve every ⚠ in the classification table (kernel-warm each
   RT/Realtek card, confirm the replug modal + cold RX); the two-Ralink shared-module test (RT5372 + PAU09);
   update the affected `<CHIP>.md` docs. The two `BUGS.md` entries were removed at the Lead's request
   (2026-07-08) ahead of HW — the fixes are code-complete and BUGS.md tracks *open* items; this plan is now
   the sole record of the remaining HW verification.

Full suite: 1432 passed. The two Linux-specific bugs are fixed in code; they await the HW sweep before the
`BUGS.md` entries come out.

## Decisions — all resolved with Lead
1. ✅ **Flag semantics (A1):** keep `LINUX_REPLUG_AFTER_MODPROBE`, flip its default to `True`, explicit
   `= False` on the three self-colders. (Not a new flag name.)
2. ✅ **Blacklist scheme (B):** keep per-chipset `.conf` files; modprobe's union of `wifit3-*.conf` is the
   reference count. No per-module files, no migration, no install-time family messaging.
3. ✅ **Replug modal UX (A3):** fully automatic (poll unplug→replug→auto-connect) with a Skip escape.
4. ✅ **Classification (A1):** default = replug-required; only `ar9271_v2`/`mt76x0u`/`mt76x2u` opt out (their
   chip docs describe an explicit self-cold mechanism). Uncertainty resolves safely toward replug — worst case
   is an unnecessary replug, never degraded RX. HW sweep confirms the ⚠ rows.
5. ✅ **Uninstall radius (B2):** two buttons when siblings exist — narrow (this chipset) and wide (this + N
   direct siblings, fully un-handed). Both touch only wifit3 files. Siblings are non-transitive.
