# Running wifit3 in Termux (Android)

**Status: experimental.** wifit3 was built for desktop Linux, Windows, and macOS. This port runs the
same Python code natively in Termux, using Android's USB host API through `termux-usb`. There is no
VM, no proot, and no root.

## Requirements

- An Android device with USB-OTG support
- Termux and the **Termux:API app** (from F-Droid or Google Play; the `termux-api` package alone is not enough)
- A supported USB adapter (for example the TP-Link Archer T2U+ / RTL8821AU, `2357:0120`)
- A Termux `libusb` build that understands `TERMUX_USB_FD` (the patch landed in late 2024)

Install the packages:

```bash
pkg install python libusb termux-api git uv
```

`pkg install termux-api` is only the CLI client; install the Termux:API app from F-Droid or Google
Play as well.

## Install

### Prebuilt wheel

Each release attaches a pure-Python wheel, so the phone needs no compiler or checkout:

```bash
pkg install python libusb termux-api
pip install ./wifit3-<version>-py3-none-any.whl   # from the release assets
```

It uses the system libusb from `pkg install libusb`. The wheel is built with `hatch_build.py`'s
Termux metadata, so it installs on any Python the project supports (3.11+); the 3.13+ note under
*From source* only applies to `uv sync`.

### From source

```bash
git clone https://github.com/derv82/wifit3
cd wifit3
uv sync
```

Termux's Python 3.13+ reports `sys.platform == "android"`, so `libusb-package` is marked
`sys_platform != "android"` and `uv sync` skips it; wifit3 then uses the system libusb from
`pkg install libusb`. Older Termux Pythons report `linux`, so the marker does not exclude it; use
`uv sync --no-install-package libusb-package` (or `pip install .`, where `hatch_build.py` drops the
dependency from the built wheel).

## Run

Start wifit3 like on any other platform:

```bash
uv run wifit3
```

If you installed with pip (the prebuilt wheel or `pip install .`), the bare `wifit3` command works.
The examples below use `uv run`; drop it when using a pip install.

No adapter is needed to launch: the splash keeps scanning, and Android asks for permission when
a card appears. Accept that dialog, and tick "Use by default for this USB device" if Android offers
it, so a replug needs no confirmation.

`wifit3` requests descriptors in a background watcher, so the UI stays responsive and no separate
launcher script is needed. Each granted fd is passed to its own libusb context, which keeps the
real terminal attached to Textual.

### Listing devices

```bash
uv run wifit3 --list-usb       # paths Android currently exposes
```

Every attached adapter is requested in the background as it appears. Each granted fd gets its own
libusb context and the contexts are aggregated, so the multi-card features work exactly as they do
on desktop.

If the adapter is unplugged, wifit3 keeps running. It reports the card as gone, and when the
adapter returns the app re-requests the descriptor and brings the card back up — the same
departure/arrival flow as desktop. If you deny a permission dialog, wifit3 backs off for 30 seconds
(up to 10 minutes) instead of re-prompting in a loop; relaunching clears the backoff.

## What differs from desktop Linux

- **No udev, no modprobe.** Android's `UsbHostManager` owns device claiming; the permission dialog is
  the only "setup" step. `SetupTermux` replaces `SetupLinux`.
- **One context per granted fd.** Termux's patched libusb reads `TERMUX_USB_FD` only at init, so each
  adapter gets a fresh context and the contexts are aggregated. A single background watcher refreshes
  the list, so UI threads never shell out to `termux-usb`.
- **No kernel driver detach.** wifit3's userland drivers want a cold-boot card. Most stock Android
  kernels ship no `rtw88`/`ath9k_htc` USB drivers, so the dongle stays cold. If your kernel does bind
  one, the port may fail on that ROM.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Splash shows no devices | Plug in the adapter and accept Android's permission dialog; `uv run wifit3 --list-usb` shows what Android exposes. |
| `termux-usb -l` does not list the card | Check OTG support; if the phone's port cannot supply enough current, try a powered hub or OTG Y-cable. |
| `system libusb not found` | `pkg install libusb`. |
| `termux-usb` is missing or the splash never sees a card | Install the Termux:API app (F-Droid/Play) and `pkg install termux-api`; the package alone is not enough. |
| `uv sync` fails on `libusb-package` (no wheel for the current platform) | Python < 3.13 on Termux reports `sys.platform == "linux"`, so the Android marker does not skip it. Run `uv sync --no-install-package libusb-package`, `pip install .` (the build hook drops it), or `pkg upgrade python`. |
| Permission dialog keeps appearing | Tick "Use by default for this USB device"; after a denial wifit3 waits 30 s before retrying. |
| Adapter disappears mid-run | Often power-related: try a powered hub or OTG Y-cable. |
| Driver reports a warm/claimed device | The ROM bound a kernel driver; test on another ROM or use a rooted setup. |

## Notes

- Termux reports `sys.platform == "linux"`, so detection uses `TERMUX_VERSION` (see
  `src/wifit3/host.py`).
- The platform code is `host.py`, `device/backend.py`, `device/termux_usb.py`, `setup/termux.py`,
  plus the bootstrap in `__main__.py` and backend wiring in `device/manager.py`.
- CI runs the full unit suite with the Termux detection env set, and checks that a Termux-built
  wheel omits `libusb-package`.
- Upstreaming is not done. This branch is a working starting point, not a supported release.
