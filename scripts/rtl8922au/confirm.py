"""Isolate the responder-arming step: A=no filter write (baseline), B=re-write the exact current
filter value, C=write _FLTR_BASE (my value, clears MPDU-max-len). All with SMA=forged, sniffer on."""
import argparse
import asyncio

import _amlib as L
from wifit3.dot11 import build_deauth
from wifit3.chips.rtl8922au.constants import R_BE_RX_FLTR_OPT

FORGED = bytes.fromhex("02acac000001")


async def run(dut, prober, n, mode):
    t, ep = dut.driver.transport, dut.driver._h2c_ep
    L.program_sma(t, ep, FORGED, net_type=L.NET_NO_LINK)
    if mode == "B":                                    # re-write the exact current value
        for reg in (R_BE_RX_FLTR_OPT, R_BE_RX_FLTR_OPT + 0x4000):
            t.write32(reg, t.read32(reg))
    elif mode == "C":                                  # my _FLTR_BASE (sniffer on)
        L.set_sniffer(t, True)
    await asyncio.sleep(0.1)
    rx = [0]                                           # DUT RX callback (matches ack_nosniffer)
    dut.driver.register_rx_callback(
        lambda p: rx.__setitem__(0, rx[0] + 1)
        if len(p.raw) >= 16 and bytes(p.raw[10:16]) == L.PROBE_SRC else None)
    prober.driver._our_tx_macs.add(L.PROBE_SRC)
    base = prober.driver.acks_seen(L.PROBE_SRC)
    frame = build_deauth(FORGED, L.PROBE_SRC, FORGED, reason=7)
    for _ in range(n):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)
    acks = prober.driver.acks_seen(L.PROBE_SRC) - base
    dut.driver.register_rx_callback(lambda p: None)
    L.restore_monitor(t, ep)
    await asyncio.sleep(0.2)
    return rx[0], acks


async def main(a):
    _ifaces, dut, prober = L.pick()
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    await prober.driver.enable_rx_acks()
    print(f"card efuse MAC = {dut.driver.mac_address}\n")
    for mode, desc in (("A", "no filter write        "),
                       ("B", "re-write exact value    "),
                       ("C", "write _FLTR_BASE (my val)")):
        recv, acks = await run(dut, prober, a.count, mode)
        print(f"[{mode}: {desc}]  DUT_recv={recv:3d}  ACKed={acks:3d}/{a.count}")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100)
    asyncio.run(main(p.parse_args()))
