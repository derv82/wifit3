import asyncio
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager

async def test_hw():
    logging.basicConfig(level=logging.INFO)
    print("[*] Starting Framework Hardware Test...")
    
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
    
    print("[*] Attempting to set Channel 6...")
    success = await iface.set_channel(6)
    if success:
        print("[+] Successfully tuned to Channel 6!")
    else:
        print("[-] Failed to tune to Channel 6.")

    print("[*] Waiting 5 seconds to observe traffic...")
    await asyncio.sleep(5)

    print("[*] Closing interface...")
    await iface.close()
    print("[+] Test complete.")

if __name__ == "__main__":
    try:
        asyncio.run(test_hw())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[-] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
