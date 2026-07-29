# Linux device permissions: current model, the problem, and a per-module rebuild

**Status:** design notes for a future build-out. The "Current model" section describes shipped code
(cited by file:line); everything from "The problem" onward is a proposal, not implemented.

**Audience:** assumes little Linux driver knowledge. The Background section starts from scratch.

This is a feature build-out, not a bug fix. It should land **before** the Phase 2 multi-card
install/uninstall batching, because batching on top of the current model multiplies the confusion it
already produces.

---

## 1. Why wifit3 touches the OS at all

wifit3 drives the card from userland and replays a recorded cold-boot byte sequence against it. The
moment a Wi-Fi card enumerates, the Linux kernel binds its driver and uploads firmware, which changes
the card's state away from the cold boot the port expects. A device-access rule alone does not stop
that: it only changes who can open the device node. So wifit3 needs two things on Linux, together:

1. **Stop the kernel driver from binding** (so the card stays in its cold, un-tainted state).
2. **Grant the user access to the raw USB node** (so wifit3 can open it without `sudo`).

These map to the two mechanisms below.

---

## 2. Background: the two Linux mechanisms

### 2a. udev rule (who may open the device node)

Every USB device shows up as a file under `/dev/bus/usb/...`. By default only `root` may open it for
writing. A **udev rule** is a small text file in `/etc/udev/rules.d/` that tells the system "when a
device with this VID:PID appears, set its node's group and permission bits so this user can open it."

A udev rule matches a **specific device** by its USB vendor/product id:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="148f", ATTR{idProduct}=="5372", GROUP="sudo", MODE="0660"
```

That line grants the `sudo` group read/write on any `148f:5372` card. It says nothing about any other
VID:PID. **udev is per-VID:PID.**

### 2b. modprobe blacklist (which kernel driver binds the card)

A **kernel module** is the driver `.ko` the kernel loads to run a device (for many Ralink USB cards
that module is `rt2800usb`). A file in `/etc/modprobe.d/` can `blacklist` a module so it is not
auto-loaded, and `install <mod> /bin/true` so nothing pulls it back in by dependency:

```
blacklist rt2800usb
install rt2800usb /bin/true
```

One module drives a **whole family** of different cards. `rt2800usb` binds many distinct VID:PIDs
(the Ralink RT30xx/RT35xx/RT53xx line). Blacklisting it stops the kernel from grabbing **every** card
in that family, not just one. **modprobe is per-kernel-module, and a module covers many VID:PIDs.**

### 2c. The asymmetry that causes everything below

- udev access is granted **per VID:PID**.
- The kernel block is applied **per module**, and one module spans many VID:PIDs.

If the two are keyed differently, a card can end up blocked from the kernel (so it stays cold) yet
without a userland access rule (so wifit3 still cannot open it). That half-state is the bug.

---

## 3. Current model (as built)

Code: `src/wifit3/setup/linux.py`, `src/wifit3/setup/__init__.py`.

**The opt-in unit is one wifit3 driver.** `SetupTarget` (`setup/__init__.py:13`) is built by
`target_for_vidpid` (`setup/__init__.py:28`) from a single driver class: `key` is the registry name
(e.g. `"rt5372"`), `ids` is that one driver's `SUPPORTED_IDS`.

**Each target writes one file pair, named by the driver key** (`setup/linux.py:72`, `:77`):

- `/etc/udev/rules.d/60-wifit3-<key>.rules` : one `SUBSYSTEM=="usb"...` line per VID:PID in
  `target.ids` (`emit_udev_text`, `setup/linux.py:199`).
- `/etc/modprobe.d/wifit3-<key>.conf` : `blacklist`/`install` for the modules discovered for the card
  (`emit_blacklist_text`, `setup/linux.py:214`).

**The blacklisted module list is discovered live** at install time from the plugged-in card:
sysfs bound driver, plus `modprobe -R` on the card's modaliases, plus the driver's
`CONFLICTING_LINUX_MODULES` as a fallback hint (`discover_kernel_modules`, `setup/linux.py:188`).
Live discovery is deliberate: it reflects what is actually installed (mainline vs DKMS) instead of a
static list that rots.

**Install and uninstall each run under one elevation** (`pkexec`/`sudo`): `install_rule`
(`setup/linux.py:452`) stages the two files and `install`s them in one `sh -c`; `remove_rule`
(`setup/linux.py:505`) `rm`s them.

**Uninstall already reference-counts shared modules.** Because several driver keys can each blacklist
the same module, `plan_uninstall` (`setup/linux.py:416`) scans wifit3's own `*.conf` files, finds the
**sibling** targets that share a module (`SiblingConf`, `setup/linux.py:337`), and the splash offers a
"narrow" (this key only) or "wide" (this key plus siblings) removal. A narrow removal that leaves the
module still blocked by a sibling reports which chipset is still blocking it (`_residual_blocked`,
`setup/linux.py:435`).

---

## 4. The problem: the file grain is finer than the block grain

Files are keyed per **wifit3 driver**. The kernel block is per **module**, and one module backs
several wifit3 drivers. So installing one card grants udev access to only that driver's VID:PIDs while
blocking the kernel for the entire module family.

### Walkthrough (the Ralink `rt2800usb` family)

`rt3070`, `rt5370`, `rt5372`, `rt5572`, ... are separate wifit3 drivers, each its own `SetupTarget`,
but on most kernels they are all bound by the single module `rt2800usb`.

1. Install an `rt5372` card. This writes `60-wifit3-rt5372.rules` (rt5372's VID:PIDs) and
   `wifit3-rt5372.conf` (`blacklist rt2800usb`).
2. `rt2800usb` is now blocked for the **whole family**. Good: those cards stay cold.
3. Plug in a *different* card, an `rt3070`. Its kernel driver is blocked (the blacklist is
   module-wide), so it stays cold. But there is **no udev rule for the rt3070 VID:PID** (only rt5372's
   was written). wifit3 cannot open it.
4. Installing the rt3070 then writes a *second* conf that also blacklists `rt2800usb` (redundant),
   plus rt3070's own udev rule.

The result is the half-state from 2c: a family member the kernel has released but wifit3 still cannot
open, plus redundant, overlapping blacklist files. The uninstall siblings machinery (section 3) is a
patch over the removal side of exactly this, and it is why removing one card can fan out to removing
several. That fan-out is not wrong, but it is surprising, and nothing on the **install** side closes
the access hole in the first place.

---

## 5. The staleness problem: buckets grow between releases

Even a per-module rewrite (section 6) has to handle version drift. Suppose wifit3 v1 wrote
`60-wifit3-rt2800usb.rules` listing the VID:PIDs it supported then. wifit3 v2 adds support for a new
card in the same family. On a machine that installed under v1:

- The blacklist already covers the new card (it is module-wide).
- The udev rule does **not** list the new VID:PID.

So the new card lands in the same half-state. Existence of the rule files is not enough. Bring-up has
to ask "are the installed rules **current** for this card," not just "do they exist," and offer to
rewrite when they are stale.

---

## 6. Target model: one file pair per kernel module

Key the files by the **kernel module**, not the wifit3 driver:

- `/etc/udev/rules.d/60-wifit3-<module>.rules` : a `SUBSYSTEM=="usb"...` line for **every**
  wifit3-supported VID:PID whose driver is bound by `<module>`.
- `/etc/modprobe.d/wifit3-<module>.conf` : `blacklist`/`install` for `<module>` (and any sibling
  modules it drags in).

**Install** = overwrite the module's pair with the full current family set (idempotent). **Uninstall**
= `rm` the pair; the whole family returns to the kernel. One elevation either way. The half-state
stops being representable: a module is either handed over (family blacklisted **and** all family
VID:PIDs granted) or not.

### 6a. What this simplifies

The reference-counting machinery (`SiblingConf`, `plan_uninstall`, `_residual_blocked`,
`confs_blacklisting`) exists only because two files could blacklist the same module. With one file per
module that cannot happen, so that whole subsystem can be deleted. Uninstall becomes: remove this
module's pair.

### 6b. What this requires (the hard part)

To write "every VID:PID that `<module>` binds," we need a **module to VID:PID** map that works even
for family members that are **not plugged in**. Today that map does not exist as static data:

- `CONFLICTING_LINUX_MODULES` is empty for every driver except `ar9271_v2`
  (`chips/ar9271_v2/driver.py:46`); the base default is `[]` (`chips/driver.py:80`).
- The module set is discovered **live** from the plugged card. Live discovery cannot enumerate the
  other, unplugged members of a family, and it is the only source today.

So the build-out has to introduce an authoritative per-driver "which kernel module binds this
silicon" declaration, then invert it to `module -> {VID:PIDs}`. Options, each with a cost:

1. **Promote `CONFLICTING_LINUX_MODULES` to authoritative and fill it in for every driver.** Simple to
   invert, but it is a hand-maintained list that can drift from reality (mainline vs DKMS module names
   differ; e.g. Realtek out-of-tree `8814au` vs in-tree naming), which is the exact rot the current
   live-discovery design set out to avoid.
2. **Keep live discovery as the truth for the plugged card, and use the static map only to name the
   file (the bucket key) and to enumerate the family for the udev rule.** The conf then blacklists the
   union of (static hint) and (live-discovered modules), so a DKMS module name that only appears live
   still gets blocked. This is the recommended blend: static map for grouping, live discovery for the
   actual block.
3. **Derive the family from the kernel's own alias table** (`modprobe -R <modalias>` across the
   family), computed at install for whatever is plugged, and stored in the file. Robust for present
   cards, still blind to absent ones.

Recommendation: option 2. The static map is used only to decide **which VID:PIDs share a file** and
**what to name it**; the live discovery still decides **what to actually blacklist on this machine**.
Getting the grouping slightly wrong (a VID:PID filed under the wrong module) is a correctness bug to
fix, not a silent hazard, because install is an idempotent full rewrite.

### 6c. Staleness handling

Stamp each generated file with a small header: the wifit3 version (or a content hash) and the exact
VID:PID set it covers. At bring-up, when a card in a handled module's family fails to open:

- If no wifit3 file exists for its module: offer a fresh install (section 6 write).
- If a file exists but does **not** list this card's VID:PID (or the stamp is older than the current
  bucket): the rules are **stale**. Offer "update," which is the same idempotent rewrite with the
  current full family set. One elevation.

Because install is a full rewrite, "update" and "install" are the same operation; only the trigger
differs.

---

## 7. Bring-up integration

`bringup.py` classifies a card that raises `BringUpPermissionsError`. Today it routes straight to
`Setup.install`. Under this model the classify step should distinguish:

- **not set up** (no file for the module) -> install,
- **stale** (file exists, missing this VID:PID / old stamp) -> update (same call),
- **set up but access not applied yet** (file current, node not writable) -> the existing
  `_wait_for_access` replug path.

For the Phase 2 batch flow, `setup_required` should be keyed by **module**, not VID:PID: two family
members needing setup collapse to one file write.

---

## 8. UI implications

- **Windows stays 1:1.** One device = one driver = one install/uninstall. No fan-out.
- **Linux is inherently per-module-family.** With one file per module, "uninstall this card" and
  "uninstall this family" are the same action, so the confusing narrow/wide split goes away. The
  confirm dialog should state the blast radius plainly, e.g. "This returns all Ralink rt2800usb cards
  to the kernel," and list the affected models. The fan-out becomes expected and labeled instead of
  hidden.
- For the Phase 2 multi-card checkbox flow, "uninstall the checked set" reduces to "remove the union
  of the checked cards' modules." Deduping by module means checking three cards of one family is one
  file removal, and the confirm can say so.

---

## 9. Migration

Users upgrading from the current per-driver files (`60-wifit3-rt5372.rules`, ...) will have stale,
finer-grained files. The install/update path should also remove any old per-driver wifit3 files it is
superseding when it writes the per-module file, so a machine ends up with exactly one file pair per
module. This can be a one-time reconciliation inside the same elevation.

---

## 10. Open questions

1. Bulk uninstall target (Phase 2): the **checked set** vs staying highlighted-row-only. Per-module
   removal makes "checked set" cleaner, but it is still a wide action that needs a clear confirm.
2. Windows batch uninstall: one elevation vs a per-card loop (feasibility TBD; `pnputil` per card
   today).
3. Where the static "driver -> module" map lives and how it is kept correct across kernel/DKMS
   variance (the section 6b risk). A test that every driver either declares a module or is covered by
   live discovery would catch omissions.
4. Do we ever need to blacklist a module we do not have a driver for (a family member wifit3 does not
   support but that shares the module)? If so the udev grant cannot cover it, and that card is blocked
   with no wifit3 use. Probably acceptable, but worth stating in the confirm copy.

---

## 11. Sequencing

1. This Linux per-module consolidation (sections 6-9).
2. Then Phase 2 batching (single-elevation multi-card install; checked-set uninstall), which becomes
   much simpler once install/uninstall are atomic per module.
