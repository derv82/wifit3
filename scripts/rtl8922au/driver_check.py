"""Verify the DRIVER's enter_active_monitor (not the scratch program_sma) auto-ACKs, with DUT RX
health visible so wedged windows are distinguishable from real negatives."""
import argparse
import asyncio

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")


async def main(a):
    _ifaces, dut, prober = L.pick()
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    await prober.driver.enable_rx_acks()
    armed = await dut.driver.enter_active_monitor(FORGED)          # <-- the real driver method
    print(f"armed SMA = {armed.hex()}")
    rx = [0]
    dut.driver.register_rx_callback(
        lambda p: rx.__setitem__(0, rx[0] + 1)
        if len(p.raw) >= 16 and bytes(p.raw[10:16]) == L.PROBE_SRC else None)
    prober.driver._our_tx_macs.add(L.PROBE_SRC)
    base = prober.driver.acks_seen(L.PROBE_SRC)
    frame = build_deauth(FORGED, L.PROBE_SRC, FORGED, reason=7)
    for _ in range(a.count):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)
    acks = prober.driver.acks_seen(L.PROBE_SRC) - base
    await dut.driver.exit_active_monitor()
    print(f"DUT_recv={rx[0]}  ACKed={acks}/{a.count}  "
          + ("<-- driver enter_active_monitor WORKS" if acks > a.count * 0.3
             else "(RX wedged, re-run)" if rx[0] == 0 else "<-- received but no ACK"))
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100)
    asyncio.run(main(p.parse_args()))
