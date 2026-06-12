"""Warm-reattach step C: confirm the FULL light reattach on the pristine warm card
(still monitor CH1 from step A; EP 0x84 RX confirmed alive by step B). NO MISC read,
NO reset, NO reload, NO clear_halt. Just: claim if3, start the RX reader (parse real
frames), then send ONE set_channel MCU command on EP 0x08 to confirm the bulk-OUT pipe
is alive too. If both work, warm reattach = light attach, no boot."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
import libusb_package, usb.core, usb.util
from wifit3.chips.mt7921au.transport import MT7921AUTransport
from wifit3.chips.mt7921au import mcu, rx
from wifit3.chips.mt7921au.constants import EP_OUT_MCU
from wifit3.wlan.packet import WlanFrameParser

def summarize(frames):
    c = {}
    for f in frames:
        t = f.get("type", "?"); c[t] = c.get(t, 0) + 1
    return c

async def main():
    backend = libusb_package.get_libusb1_backend()
    d = usb.core.find(idVendor=0x0e8d, idProduct=0x7961, backend=backend)
    if d is None: print("no device"); return
    print(f"warm card addr={d.address}")
    usb.util.claim_interface(d, 3)   # minimal: claim only. NO MISC, NO clear_halt, NO reset.
    t = MT7921AUTransport(d)
    parser = WlanFrameParser()
    frames = []
    def on_raw(data):
        dec = rx.decode_frame(data)
        if dec is None: return
        off, end, rssi, fcs = dec
        if fcs: return
        fb = data[off:end]
        if len(fb) < 10: return
        try:
            p = parser.parse_80211_frame(fb, rssi)
            if p: frames.append(p)
        except Exception:
            pass
    t.subscribe(on_raw)
    t.start_rx()
    print("RX reader attached (NO boot). Listening 2s...")
    await asyncio.sleep(2)
    print(f"  parsed {len(frames)} frames: {summarize(frames)}")

    print("testing EP 0x08 bulk-OUT: set_channel(6) MCU command...")
    cmd, payload = mcu.config_sniffer(6)
    frame, seq = t._build_mcu_frame(cmd, payload)
    sent = await t.send_bulk_checked(frame, EP_OUT_MCU)
    print(f"  EP 0x08 send -> {'OK (bulk-OUT ALIVE)' if sent else 'STALLED'}")

    frames.clear()
    await asyncio.sleep(2)
    print(f"  after set_channel(6): parsed {len(frames)} frames: {summarize(frames)}")
    await t.stop_rx()
    print("DONE.")

asyncio.run(main())
