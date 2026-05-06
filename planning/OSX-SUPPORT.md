Q: "Can OSX users just run `rmmod` like on Linux?"

macOS is the absolute hardest environment for hardware bypasses because Apple locks down the USB and networking stacks tighter than Fort Knox. 

You cannot use `rmmod` on macOS because macOS doesn't use Linux loadable kernel modules (LKMs). Instead, it uses **Kernel Extensions (Kexts)** or, in newer macOS versions (Catalina and later), **SystemExtensions / DriverKit**.

If macOS automatically claims your USB Wi-Fi adapter with its own driver, PyUSB will throw an `Access Denied` or `Resource Busy` error when you try to run your `dev.set_configuration()` or write to the endpoints.

Here is exactly how you handle macOS, broken down by modern vs. legacy versions.

### 1. The Modern macOS Way (Catalina 10.15 and Later)

Apple deprecated Kexts for most USB devices and moved to user-space DriverKit. Unfortunately, they also aggressively tightened security via **System Integrity Protection (SIP)** and the **Entitlements** system.

**The "Codeless Kext" Hack (The Zadig of macOS)**
You can't just "unload" a modern Apple-provided driver easily without disabling SIP (which you shouldn't ask users to do). Instead, you use a trick called a "Codeless Kext."

A Codeless Kext is a dummy driver file (`.kext`) that contains zero executable code. It only contains an `Info.plist` file. In that file, you specify the `idVendor` (0x0bda) and `idProduct` (0x8187) of your Alfa card, and you set a very high `IOProbeScore`.

When you plug in the card, macOS's kernel matching system reads the dummy driver, sees the high score, and assigns the card to the dummy driver instead of the real Apple driver. Since the dummy driver does nothing, the USB interface is left completely unconfigured and unclaimed—perfectly primed for PyUSB to swoop in and grab it via `libusb`.

### 2. The Legacy macOS Way (Mojave 10.14 and Older)

On older Macs, the OS uses traditional Kernel Extensions. The equivalent of Linux's `rmmod` is `kextunload`.

If the kernel has grabbed the Realtek driver (e.g., `com.realtek.driver.RtWlanU`), you can forcefully unload it from the terminal:

```bash
sudo kextunload -b com.realtek.driver.RtWlanU
```
*(You would need to find the exact bundle identifier using `kextstat | grep -i realtek` or similar).*

After running that command, the OS releases the device, and PyUSB can claim it.

### The PyUSB `detach_kernel_driver` Function

Regardless of the OS, PyUSB has a built-in function meant to handle this automatically: `dev.detach_kernel_driver(interface_number)`.

However, its implementation varies wildly:
* **Linux:** It works flawlessly. It essentially issues a USB `IOCTL_USB_DISCONNECT` to the kernel, forcing it to unbind the driver (equivalent to what `rmmod` achieves, but specific to that one device).
* **macOS:** It historically **fails or is completely unimplemented** in the macOS `libusb` backend due to Apple's strict kernel APIs. You almost always have to rely on the Codeless Kext method or manual `kextunload`.
* **Windows:** It is unimplemented. You must use Zadig to swap to WinUSB.

### The Summary for your Wifite3 User Flow

If you want true cross-platform support without forcing users to disable macOS System Integrity Protection:

1.  **Windows:** User runs Zadig to swap to WinUSB.
2.  **Linux:** Script runs `dev.detach_kernel_driver(0)` natively in PyUSB (or asks the user to `sudo rmmod rtl8187` if that fails).
3.  **macOS:** You provide a pre-compiled "Codeless Kext" in your repo. The user installs it once, which permanently prevents macOS from claiming that specific Alfa card, leaving it open for your script.