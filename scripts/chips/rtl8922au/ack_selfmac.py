"""Test 1 - does programming the addr-cam SMA = a REAL MAC make the card HW auto-ACK?

The unexplained gap vs every prior RTL: rtl8xxxu holds the perm MAC in REG_MACID from boot and
self-ACKs for free, but the rtw89 monitor vif comes up with addr-cam SMA = 00:00:00:00:00:00, so
the hardware currently has no self-address at all. This programs SMA = the card's efuse MAC (and a
forged MAC, and a zero control), injects unicast frames to that MAC from the 8812 prober, and counts
the ACKs that come back to the prober. A non-zero row means the CAM SMA DOES arm the responder and
active monitor is within reach; all zero means the CAM SMA is not the lever (go to Test 2, a1match).

  uv run python scripts/chips/rtl8922au/ack_selfmac.py [--reset] [--count 100]

Run right after a physical replug (fresh card). rx_autoack's silicon-MAC row was misleading because
it probed the silicon MAC AFTER the SMA was reset to 0; this programs it and probes it together.
"""
import argparse
import asyncio

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")


async def acks_for(prober, dut, sma, dst, n, net_type):
    L.program_sma(dut.driver.transport, dut.driver._h2c_ep, sma, net_type=net_type)
    await asyncio.sleep(0.1)
    src = L.PROBE_SRC
    prober.driver._our_tx_macs.add(src)
    frame = build_deauth(dst, src, dst, reason=7)     # a1=dst (the DUT's SMA), a2=PROBE_SRC
    base = prober.driver.acks_seen(src)
    for _ in range(n):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)                           # let the last ACKs land on the prober tap
    return prober.driver.acks_seen(src) - base


async def main(a):
    if a.reset:
        L.reset_device()
    _ifaces, dut, prober = L.pick()
    print(f"DUT={dut.description}  prober={prober.description}")
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    await prober.driver.enable_rx_acks()
    silicon = L.silicon_mac(dut)
    print(f"card efuse MAC = {dut.driver.mac_address}\n")

    cases = [
        ("SMA=silicon  net=NO_LINK", silicon, silicon, L.NET_NO_LINK),
        ("SMA=silicon  net=INFRA  ", silicon, silicon, L.NET_INFRA),
        ("SMA=forged   net=NO_LINK", FORGED,  FORGED,  L.NET_NO_LINK),
        ("SMA=forged   net=INFRA  ", FORGED,  FORGED,  L.NET_INFRA),
        ("SMA=0        (control)  ", b"\x00" * 6, FORGED, L.NET_NO_LINK),
    ]
    for label, sma, dst, net in cases:
        acks = await acks_for(prober, dut, sma, dst, a.count, net)
        verdict = "  <-- AUTO-ACK!" if acks > a.count * 0.3 else ""
        print(f"[{label}]  {acks:3d}/{a.count} ACKed{verdict}")
        L.restore_monitor(dut.driver.transport, dut.driver._h2c_ep)
        await asyncio.sleep(0.3)

    print("\nRead: any row (esp. SMA=silicon) near count means the addr-cam SMA arms the responder")
    print("      -> active monitor is reachable by programming the SMA. All ~0 -> run Test 2 (a1match).")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="USB-reset the card first (replug is better)")
    p.add_argument("--count", type=int, default=100)
    asyncio.run(main(p.parse_args()))
