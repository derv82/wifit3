"""Test 2 - does the hardware consider a programmed-SMA address "me"?

Reads the rx descriptor's own verdict on every received frame:
  BE_RXD_A1_MATCH   (W3 bit10) - hardware decided addr1 == one of my addresses
  BE_RXD_ADDR_CAM_VLD (W2 bit14) - the addr-cam lookup hit a valid entry
Sniffer mode stays ON, so frames are received regardless; we just read what the hardware stamped.

  A1_MATCH goes to 1 on to-SMA frames -> the CAM DID make the address "me"; the auto-ACK gap is a
    responder-enable, not addressing (look for a response-enable register / macid state).
  A1_MATCH stays 0 -> the addr-cam SMA is NOT the responder's match source (the search for the real
    self-address register continues; or the CAM needs different programming).

  uv run python scripts/rtl8922au/a1match.py [--reset] [--count 80]

The BASELINE broadcast phase must show total_wifi > 0, else the tap/card is dead (replug). The tap
wraps _rx_dispatch BEFORE connect so the shared RxReaderThread captures it (a direct bulk read
starves RX on this chip).
"""
import argparse
import asyncio
import struct

import _amlib as L
from wifit3.dot11 import build_deauth

FORGED = bytes.fromhex("02acac000001")
BCAST = b"\xff" * 6
_st = {"match": None, "tot": 0, "n": 0, "a1": 0, "cam": 0}


def _wrap(orig):
    """Decode BE_RXD_A1_MATCH / ADDR_CAM_VLD per WIFI frame, then hand the buffer to the real
    dispatch so RX still flows to the app."""
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
            if (w0 >> 24) & 0x3F == 0:                      # WIFI pkt_type
                _st["tot"] += 1
                a2 = bytes(buf[pos + off + 10:pos + off + 16])
                if a2 == _st["match"]:
                    w2 = struct.unpack_from("<I", buf, pos + 8)[0]
                    w3 = struct.unpack_from("<I", buf, pos + 12)[0]
                    _st["n"] += 1
                    _st["a1"] += (w3 >> 10) & 1
                    _st["cam"] += (w2 >> 14) & 1
            pos += (tot + 15) & ~15
        return orig(buf)
    return w


async def phase(dut, prober, dst, label, n, sma=None, net=L.NET_NO_LINK):
    if sma is not None:
        L.program_sma(dut.driver.transport, dut.driver._h2c_ep, sma, net_type=net)
        await asyncio.sleep(0.1)
    _st.update(match=L.PROBE_SRC, tot=0, n=0, a1=0, cam=0)
    a3 = dst if dst != BCAST else L.PROBE_SRC
    frame = build_deauth(dst, L.PROBE_SRC, a3, reason=7)
    for _ in range(n):
        await prober.driver.inject_frame(frame)
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)
    if sma is not None:
        L.restore_monitor(dut.driver.transport, dut.driver._h2c_ep)
    print(f"[{label}] total_wifi={_st['tot']:4d}  from_probe={_st['n']:3d}  "
          f"A1_MATCH={_st['a1']:3d}  ADDR_CAM_VLD={_st['cam']:3d}")


async def main(a):
    if a.reset:
        L.reset_device()
    _ifaces, dut, prober = L.pick()
    dut.driver._rx_dispatch = _wrap(dut.driver._rx_dispatch)     # before connect: reader captures it
    await asyncio.gather(dut.connect(), prober.connect())
    await asyncio.gather(dut.set_channel(1), prober.set_channel(1))
    dut.driver.register_rx_callback(lambda p: None)             # arm the normal dispatch tail too
    silicon = L.silicon_mac(dut)
    print(f"card efuse MAC = {dut.driver.mac_address}\n")

    await phase(dut, prober, BCAST, "BASELINE bcast (tap check)", a.count)
    await phase(dut, prober, FORGED, "SMA=forged  net=NO_LINK  ", a.count, sma=FORGED, net=L.NET_NO_LINK)
    await phase(dut, prober, silicon, "SMA=silicon net=NO_LINK  ", a.count, sma=silicon, net=L.NET_NO_LINK)
    await phase(dut, prober, silicon, "SMA=silicon net=INFRA    ", a.count, sma=silicon, net=L.NET_INFRA)

    print("\nRead: BASELINE total_wifi>0 confirms the tap. Then on the SMA rows, A1_MATCH>0 means the")
    print("      CAM made the address 'me' (auto-ACK gap = responder-enable); A1_MATCH=0 means it did not.")
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="USB-reset the card first (replug is better)")
    p.add_argument("--count", type=int, default=80)
    asyncio.run(main(p.parse_args()))
