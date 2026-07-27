"""Verify active monitor stays a real MONITOR: with the addr-cam SMA armed, the card must still
receive foreign traffic (frames NOT addressed to us, incl. toDS client->AP frames) AND must ACK
only our SMA, not the foreign frames.

Taps the raw rx path (counts by addr1) and the prober's ACK tally for two source MACs.

  uv run python scripts/rtl8922au/monitor_check.py [--count 100]
"""
import argparse
import asyncio
import struct

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")     # our armed SMA (to-us)
FOREIGN_BSSID = bytes.fromhex("02dede000009")   # a foreign AP (addr1 of a toDS frame; NOT us)
CLIENT = bytes.fromhex("02cccc000002")     # the foreign client (addr2 of the toDS frame)
_c = {"total": 0, "to_us": 0, "foreign": 0}


def toDS(bssid, client, dest):
    """A minimal data frame with toDS=1: addr1=bssid (RA=AP, not us), addr2=client, addr3=dest."""
    return bytes([0x08, 0x01]) + b"\x00\x00" + bssid + client + dest + b"\x00\x00" + b"\x00" * 8


def _wrap(orig):
    def w(buf):
        pos, n = 0, len(buf)
        while pos + 24 <= n:
            w0 = struct.unpack_from("<I", buf, pos)[0]
            rxd = 40 if (w0 >> 31) & 1 else 24
            sz = w0 & 0x3FFF
            off = rxd + (((w0 >> 14) & 3) << 1) + (((w0 >> 18) & 3) << 3) \
                + (((w0 >> 22) & 3) << 3) + (((w0 >> 20) & 3) << 4)
            tot = off + sz
            if sz == 0 or pos + tot > n:
                break
            if (w0 >> 24) & 0x3F == 0 and sz >= 16:
                a1 = bytes(buf[pos + off + 4:pos + off + 10])
                _c["total"] += 1
                if a1 == FORGED:
                    _c["to_us"] += 1
                elif a1 == FOREIGN_BSSID:
                    _c["foreign"] += 1
            pos += (tot + 15) & ~15
        return orig(buf)
    return w


async def main(a):
    _ifaces, dut, prober = L.pick()
    dut.driver._rx_dispatch = _wrap(dut.driver._rx_dispatch)     # before connect
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    dut.driver.register_rx_callback(lambda p: None)
    await prober.driver.enable_rx_acks()
    print(f"card efuse MAC = {dut.driver.mac_address}\n")

    L.program_sma(dut.driver.transport, dut.driver._h2c_ep, FORGED, net_type=L.NET_NO_LINK)  # arm
    prober.driver._our_tx_macs.update({L.PROBE_SRC, CLIENT})
    base_us = prober.driver.acks_seen(L.PROBE_SRC)
    base_fg = prober.driver.acks_seen(CLIENT)

    to_us = build_deauth(FORGED, L.PROBE_SRC, FORGED, reason=7)     # addr1=our SMA
    to_foreign = toDS(FOREIGN_BSSID, CLIENT, FORGED)               # addr1=foreign AP, toDS
    for _ in range(a.count):
        await prober.driver.inject_frame(to_us)
        await prober.driver.inject_frame(to_foreign)
        await asyncio.sleep(0.02)
    await asyncio.sleep(1.0)
    ack_us = prober.driver.acks_seen(L.PROBE_SRC) - base_us
    ack_fg = prober.driver.acks_seen(CLIENT) - base_fg

    print(f"RX (raw tap):  total={_c['total']}  to_us(addr1=SMA)={_c['to_us']}  "
          f"foreign(addr1=other, toDS)={_c['foreign']}")
    print(f"ACKs back:     to_us={ack_us}/{a.count}   foreign={ack_fg}/{a.count}")
    print()
    mon_ok = _c["foreign"] > 0
    ack_ok = ack_us > a.count * 0.3 and ack_fg <= a.count * 0.1
    print(f"MONITOR retained (sees foreign toDS not to us): {'YES' if mon_ok else 'NO'}")
    print(f"ACK selective (ACKs our SMA, not foreign):      {'YES' if ack_ok else 'NO'}")
    if _c["total"] == 0:
        print("(total=0 -> RX wedged this run; re-run for a valid window)")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100)
    asyncio.run(main(p.parse_args()))
