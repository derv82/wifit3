"""Test 3 - fake a minimal AD_HOC (IBSS) role so the hardware responder ACKs unicast-to-self.

The gap in every earlier attempt: the responder is armed by the PORT-config register net_type
(R_BE_PORT_CFG_P0), not just the addr-cam / join_info net_type. The monitor bring-up sets the port
to NO_LINK and clears rx_sw (B_AX_RX_BSSID_FIT_EN) + TSF_UDT_EN; rtw89_mac_port_cfg_rx_sw /
rx_sync_by_nettype set those exact bits for INFRA || AD_HOC. AD_HOC is the IBSS role that ACKs
unicast-to-self with no AP and no 4-way. This flips the port to AD_HOC + those bits, programs the
addr-cam SMA (exact match), injects unicast to it from the 8812, and counts ACKs back.

  uv run python scripts/chips/rtl8922au/ack_adhoc.py [--reset] [--count 100] [--clear-cca]

--clear-cca also drops the responder's CCA check (B_BE_RSP_CHK_CCA) in case BT-coex CCA is eating
responses on this combo part. A non-zero row = active monitor is solved; wire it into the driver.
See RTL8922AU.md "Active monitor (auto-ACK): open".
"""
import argparse
import asyncio

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")


async def acks_for(prober, dut, sma, dst, n, *, adhoc, clear_cca):
    t, ep = dut.driver.transport, dut.driver._h2c_ep
    L.program_sma(t, ep, sma, net_type=(L.NET_ADHOC if adhoc else L.NET_NO_LINK))
    if adhoc:
        L.set_port_adhoc(t)
    if clear_cca:
        L.clear_rsp_cca(t)
    await asyncio.sleep(0.1)

    src = L.PROBE_SRC
    prober.driver._our_tx_macs.add(src)
    frame = build_deauth(dst, src, dst, reason=7)
    base = prober.driver.acks_seen(src)
    for _ in range(n):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)
    acks = prober.driver.acks_seen(src) - base

    if adhoc:
        L.restore_port_monitor(t)
    L.restore_monitor(t, ep)
    await asyncio.sleep(0.2)
    return acks


async def main(a):
    if a.reset:
        L.reset_device()
    _ifaces, dut, prober = L.pick()
    print(f"DUT={dut.description}  prober={prober.description}  clear_cca={a.clear_cca}")
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    await prober.driver.enable_rx_acks()
    silicon = L.silicon_mac(dut)
    print(f"card efuse MAC = {dut.driver.mac_address}\n")

    cases = [
        ("AD_HOC port  SMA=silicon", silicon, silicon, True),
        ("AD_HOC port  SMA=forged ", FORGED,  FORGED,  True),
        ("NO_LINK ctrl SMA=silicon", silicon, silicon, False),
    ]
    for label, sma, dst, adhoc in cases:
        acks = await acks_for(prober, dut, sma, dst, a.count, adhoc=adhoc, clear_cca=a.clear_cca)
        verdict = "  <-- AUTO-ACK!" if acks > a.count * 0.3 else ""
        print(f"[{label}]  {acks:3d}/{a.count} ACKed{verdict}")

    print("\nRead: an AD_HOC row near count means the port-role net_type arms the responder -> solved.")
    print("      If still 0, retry with --clear-cca, then run a1match to see if A1_MATCH now sets.")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="USB-reset the card first (replug is better)")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--clear-cca", action="store_true", help="also drop the responder CCA/BT-coex check")
    asyncio.run(main(p.parse_args()))
