The script I used to automate the data generation (`./scripts/capture.sh`) has strange timing:
1. (T=0) Tell user to begin capturing in wireshark, THEN PRESS ENTER.
  - Takes ~1-2 seconds to click start capturing, alt+tab to terminal, press enter.
2. (T=X)Script prints out ("enter wifi card to USB slot 3") and immediately starts to sleep for 10 seconds.
  - Takes ~0-2 seconds to insert USB wifi card.
  - The "firmware upload" happens during this time.
  - The whole time, the script is still in a 10-second sleep since the user last pressed ENTER.
3. (T=X+10) 10 second sleep ends. Script immediately runs airmon-ng start wlan1. blocks and waits for the command to finish. and THEN sleeps 3 seconds once airmon-ng completes execution.
4. (T=X+10+airmon+3) 3 second sleep ends. Script runs "iw dev $MON_IFACE set channel 6", blocks waiting for it to complete, THEN sleeps for 5 seconds.
5. (T=X+10+airmon+3+iw+5) 5 second sleep ends. Script runs "iw dev $MON_IFACE set channel 1", blocks waiting for completion, THEN sleeps for 5 seconds.
6. (T=X+10+airmon+3+iw+5+iw+5) Script starts as a BACKGROUND THREAD: "sudo airodump-ng --channel 1 $MON_IFACE &", (does NOT wait for it to complete, assume time taken to start bg job is 0), and then sleeps for 5 seconds.
7. (T=X+10+airmon+3+iw+5+iw+5+5) second sleep ends. Airodump is still running. Script executes "aireplay-ng --test [my access point] wlan1mon", blocks waiting for execution, then sleeps for 5 seconds.
8. (T=X+10+airmon+3+iw+5+iw+5+5+5) second sleep ends, kill airodump process, perform "Teardown" (airmon-ng stop with error handling, avoids kernel panic).

## One-off Analysis
So given this timeline, I can see the very first firmware chunk is sent at T=3.418102, last chunk is sent at T=3.671049 -- that's when the device was plugged in (this is at some arbitrary time during the "10 second sleep").

Start Wireshark Capturing (T=0),
Press ENTER (T=?),
Plug in hardware (T~=3.418102ish - plug in would happen sometime before the firmware bytes are sent).

Then a whole bunch of interrupts go from 3.4181s to 4.2297s. And then: Silence.

Silence until T=12.42315s (interrupt out). We assume this is due to `airmon-ng start wlan1`, this is *barely after* the moment the 10 second sleep ends.

*TODO: `time sudo airmon-ng start wlan1` for the ar9271.*

Airmon is probably 0.5-2.5 seconds to execute, just a safe ballpark figure.

...Lots of URB_BULK in noise after 12.42315...

Then at T=15.860959, packets switch to nothing but URB_INTERRUPTs. This must be *barely after* the moment the 3 second sleep ends (after airmon-ng finishes). These INTERRUPTs are the channel hopping bytes (we used "iw" to change to channel 6).

...Lots of INTERRUPTs, then BULK in starts around T=15.99640, lots of BULK ins...

Then at T=21.008466, packets witch back to nothing but URB_INTERRUPTs again. It's been ~5 seconds + some change, so this must be *barely after* the `iw` command to change to channel 1.

...Lots of INTERRUPTs, then BULK starts around T=21.141523. I guess channel hopping is done?

...Just tons of BULK Ins after this. aireplay-ng is injecting at some point (T~=26?) but it's hard to see through all the BULK IN packets.