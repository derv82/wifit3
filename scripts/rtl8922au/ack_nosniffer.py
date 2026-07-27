"""Test 4 - with the addr-cam SMA proven to match (A1_MATCH=1), does clearing SNIFFER_MODE let the
responder ACK?

Hypothesis from the a1match result: the responder is enabled (B_BE_RESP_PKTCTL_EN) and the hardware
flags to-SMA frames as A1_MATCH=1, but in SNIFFER_MODE those frames are accepted *promiscuously* and
the responder skips them. Clearing sniffer forces to-SMA frames to be accepted via A1/UC-CAM match
(flagged to-me), which should let the ACK fire. This counts BOTH the DUT's received to-SMA frames
(proves sniffer-off didn't kill RX) AND the ACKs the 8812 hears back.

  uv run python scripts/rtl8922au/ack_nosniffer.py [--reset] [--count 100]
"""
import argparse
import asyncio

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")


async def run(dut, prober, sma, dst, n, *, sniffer):
    t, ep = dut.driver.transport, dut.driver._h2c_ep
    L.program_sma(t, ep, sma, net_type=L.NET_NO_LINK)
    L.set_sniffer(t, sniffer)
    await asyncio.sleep(0.1)

    rx = [0]
    dut.driver.register_rx_callback(
        lambda p: rx.__setitem__(0, rx[0] + 1)
        if len(p.raw) >= 16 and bytes(p.raw[10:16]) == L.PROBE_SRC else None)
    prober.driver._our_tx_macs.add(L.PROBE_SRC)
    base = prober.driver.acks_seen(L.PROBE_SRC)
    frame = build_deauth(dst, L.PROBE_SRC, dst, reason=7)
    for _ in range(n):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)
    acks = prober.driver.acks_seen(L.PROBE_SRC) - base
    dut.driver.register_rx_callback(lambda p: None)

    L.set_sniffer(t, True)
    L.restore_monitor(t, ep)
    await asyncio.sleep(0.2)
    return rx[0], acks


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
        ("SMA=forged  sniffer=OFF", FORGED,  FORGED,  False),
        ("SMA=silicon sniffer=OFF", silicon, silicon, False),
        ("SMA=forged  sniffer=ON (ctrl)", FORGED, FORGED, True),
    ]
    for label, sma, dst, sniffer in cases:
        got, acks = await run(dut, prober, sma, dst, a.count, sniffer=sniffer)
        verdict = "  <-- AUTO-ACK!" if acks > a.count * 0.3 else (" (RX dead)" if got == 0 else "")
        print(f"[{label}]  DUT_recv={got:3d}  ACKed={acks:3d}/{a.count}{verdict}")

    print("\nRead: DUT_recv>0 means sniffer-off kept RX; ACKed>0 there means SNIFFER_MODE was the")
    print("      responder suppressor -> active monitor solved. DUT_recv=0 means sniffer-off killed RX.")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--count", type=int, default=100)
    asyncio.run(main(p.parse_args()))
