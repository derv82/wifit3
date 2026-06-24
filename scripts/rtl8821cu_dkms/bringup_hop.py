"""Faithful RX-health harness: drive the driver the way ``uv run wifit3`` does — connect, then
CONTINUOUSLY hop across BOTH bands (repeated 5<->2.4 GHz transitions) — and classify each launch
good/dead by delivered frames, split per band. The ch1-parked loops (bringup_cointoss /
bringup_timing) never band-switch, so they miss the 5->2.4 GHz revival the real app gets and
over-report 'dead'. This one replicates the app's hop, no replug needed between launches.

Also instruments the per-boot DC-cancellation (dm._dc_cancellation): the live measurement read off
BB dbg-port 0x200, the resulting 0xc10/0xc14 compensation, and whether phydm_stop_ic_trx ever
returned FAIL (the fail-abort branch). At the end it diffs those good-vs-dead — if the DC numbers
track the good/dead split, the DC measurement is the per-boot variable.

Passive (RX only, no TX).  uv run python scripts/rtl8821cu_dkms/bringup_hop.py [iters] [secs] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import bringup, efuse, watchdog
from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.driver import CHANNELS_2G, CHANNELS_5G, Rtl8821cuDkmsDriver
from wifit3.chips.rx_reader import RxReaderThread

# Interleave the bands so the hop forces a 2.4<->5 GHz band switch on almost every step — the
# repeated transition the real app does over time, compressed. (chan.set_channel only runs the
# band-switch sub-step when the band actually changes, so interleaving maximises it.)
_HOP = [c for pair in zip(CHANNELS_2G * 2, (CHANNELS_5G * 3)[: len(CHANNELS_2G) * 2]) for c in pair]


def _install_dc_hooks() -> dict:
    """Monkeypatch the dm module to capture the DC-cancellation telemetry of the NEXT cold_bringup.
    Returns a dict the caller reads after connect(); call _reset() (in it) before each launch."""
    cap = {"dbg_reads": [], "set_fails": 0}
    orig_set, orig_get = dm_mod._set_bb_dbg_port, dm_mod._get_bb_dbg_port_val  # noqa: SLF001
    orig_stop = dm_mod.stop_ic_trx
    state = {"port": None}

    def set_hook(t, port):
        state["port"] = port
        return orig_set(t, port)

    def get_hook(t):
        v = orig_get(t)
        cap["dbg_reads"].append((state["port"], v))
        return v

    def stop_hook(t, set_type, ts):
        r = orig_stop(t, set_type, ts)
        if set_type and r is False:
            cap["set_fails"] += 1
        return r

    dm_mod._set_bb_dbg_port = set_hook        # noqa: SLF001
    dm_mod._get_bb_dbg_port_val = get_hook    # noqa: SLF001
    dm_mod.stop_ic_trx = stop_hook

    def reset():
        cap["dbg_reads"].clear()
        cap["set_fails"] = 0

    cap["_reset"] = reset
    return cap


async def one(dev, secs: float, cap: dict) -> dict:
    cap["_reset"]()
    drv = Rtl8821cuDkmsDriver(dev)
    band = {"cur": "2g", "n2g": 0, "n5g": 0}
    drv.register_rx_callback(lambda p: band.__setitem__(
        "n2g" if band["cur"] == "2g" else "n5g",
        band["n2g" if band["cur"] == "2g" else "n5g"] + 1))
    await drv.connect()
    t = drv.transport
    dc_meas = next((v for p, v in cap["dbg_reads"] if p == 0x200), None)
    c10, c14 = t.read32(0x0C10), t.read32(0x0C14)
    set_fails = cap["set_fails"]

    loop = asyncio.get_running_loop()
    end = loop.time() + secs
    i = 0
    while loop.time() < end:
        ch = _HOP[i % len(_HOP)]
        band["cur"] = "5g" if ch > 14 else "2g"
        await drv.set_channel(ch)
        await asyncio.sleep(0.25)
        i += 1
    await drv.close()
    return {"n2g": band["n2g"], "n5g": band["n5g"], "dc_meas": dc_meas,
            "c10": c10, "c14": c14, "set_fails": set_fails}


def _h(v) -> str:
    return "----" if v is None else f"0x{v:x}"


async def run(iters: int, secs: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    cap = _install_dc_hooks()
    good, dead = [], []
    for i in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            r = await one(dev, secs, cap)
        except Exception as e:  # noqa: BLE001
            print(f"iter {i:2d}: EXCEPTION {e!r}")
            await asyncio.sleep(2.0)
            continue
        total = r["n2g"] + r["n5g"]
        verdict = "GOOD" if total >= 20 else "DEAD"
        (good if verdict == "GOOD" else dead).append(r)
        print(f"iter {i:2d}: {verdict}  total={total:5d}  2g={r['n2g']:5d} 5g={r['n5g']:5d}  "
              f"dc_meas={_h(r['dc_meas'])}  0xc10={_h(r['c10'])} 0xc14={_h(r['c14'])}  "
              f"stop_fail={r['set_fails']}")
        await asyncio.sleep(rest)

    print(f"\n=== {len(good)} GOOD / {len(dead)} DEAD over {iters} launches ===")
    if good and dead:
        for key in ("dc_meas", "c10", "c14", "set_fails"):
            g = sorted({_h(d[key]) if key != "set_fails" else d[key] for d in good})
            d = sorted({_h(x[key]) if key != "set_fails" else x[key] for x in dead})
            flag = "  <-- DIFFERS" if set(map(str, g)) != set(map(str, d)) else ""
            print(f"  {key:10s} good={g}  dead={d}{flag}")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    sc = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
    raise SystemExit(asyncio.run(run(it, sc, rs)))
