## Focus View: Layout Problems

Some things I was thinking when I realized I really don't like the Focus UI:
- Top row is extremely information dense (TARGET INFO/SECURITY/CAPTURE/PACKET ACTIVITY).
  - I think SECURITY and CAPTURE could be swapped?
  - Or just.. there's gotta be a cleaner way to visualize a target, it's clients, attacks, and the live stats, attack progress...
- Logs get WAY too much horizontal space on a wide screen.
- I optimized for small terminals and a consistent overall UI look, but we made it ugly for wide screen / extremely high resolution.

Testing is just me maximizing and resizing the terminal to "a normal size":
- I think the gold standard is 80x40? or 100x80? I think we went with 120x80 maybe, as the bare-minimum supported dimensions. Everything else expands to fill.
- SSH: Just clone & build wifit3 on the pihole?
- iPhone SSH app -> Computer -> Wifit3 (Portrait + Landscape)
- I need to figure out how to size my Windows Terminal to an exact width/height.
- I need to know what are the most common Terminal resolutions (Laptop, Desktop, Mobile, Low resolution, High resolution, small fonts, big fonts)

Fluid design:
- Does Textual support "box" rendering that changes depending on Portrait/Landscape screen dimensions? Like Boostrap or whaever. Flexbox.


## Focus View: Complete Redesign?

Requirements:
- Access point information (sent by AP)
  - Static: BSSID, ESSID, Channel, Encryption/Cypher
  - Dynamic: Power, Beacons, Signal Bar, Last seen
  - Static/Dynamic? WPS Lock, Protected Management Frames (PMF).
- Clients table
  - Each client: BSSID, Power, Packet num, Fingerprint [not implemented yet] (Apple/Samsung/FireTV/Ring Camera).
  - Including buttons to deauth a specific (selected) client or "all"/broadcast.
  - Brainstorming: Maybe a red 'deauth' button next to every client BSSID (float:right), deauth button only appears on hover/selection.
  - Brainstorming: "broadcast deauth" button aligns to top of clients table.
- Attack buttons (Extract PMKID, WPA Downgrade, WPS Brute Force).
- Event Log, indicates current state (listening, attacking, cracking, cracked).
  - Most - if not all - log lines have been reduced to within a certain width ( < 50 chars?)
  - We can have a set width for the log, it doesn't have to expand.
- Packet Activity Dashboard
  - We have to keep this, it looks so cool and is great at visalizing what Wifit3 is doing.
  - Currently 5 rows high. We could split "data" to 2 separate line graphs "data" and "ivs"
  - Show be more prominent in Focus view.
- Overall: Consistent UX regardless of screen width/height
  - Gut irrelevant things when real-estate is small (the weay we collapse "PACKET ACTIVITY" right now is a good example of this)

Problems:
- Screen real estate is TIGHT. We thoroughly truncated pretty much every Log line and panel label.
  - We have shortened button labels to become basically meaningless: "Chop", "WPS PIN", "WPA[down]"
  - Panel borders & padding is eating a lot of screen real estate.\
- Showing lots of information in a super-easy-to-understand way.
- Showing lots of information that looks good on both Portrait, Mobile, high resolution (super wide & high, 200x100 or higher), low resolution (80x40).
- Multiple aeras subtly indicate when signal is dead (Last Seen=red, Signal=dimming heartbeat "X", should be more prominent.

----------

## Focus Redesign Idea #1: Visualize like a Router Admin Page

Picture of router. Picture of wireless card. List of clients. Logically grouped. Lines connecting them.

- Left side, full column: ANSI art wireless card
  - WiFI bars radiating out (always animated?)
  - Wireless card model & driver/chipset directly below card.
  - Wireless card uptime? Do we have access to this on the device? We can track it but a warm boot would lose the actual uptime, maybe we don't add this...
- Right side, lower-half: ANSI art router
  - WiFi bars radiating out (animated on beacon?).
  - Router name, BSSID directly below router.
  - Router channel, Power, Signal Bar - Directly above router, above/between antennas.
  - Security: Encryption, Cipher, Cracked Password - Underneath router name/bssid.
- Middle, Between Card and Router:
  - Top 3rd: Attack buttons, capture status
  - Middle 3rd: PAKCET ACTIVITY stretched between Card & Router, full length history, flows right-left (Card <- Router), i.e. the flow of data.
  - Bottom 3rd: Clients list, aligned directly "to the left" of the Router ANSI art.

Where does Event Log fit?
- Underneath the card? Should be enough space on lower 1/3rd, stretch to fill to the right, up to the left side of the clients table.
- Footer? That eats real estate for log lines that are never > 50 chars long.

Behavior:
- Packet Graph shows data flowing right-to-left from AP to the Card (Card <- AP)
  - Injections & Deauths should flow in the opposite direction, left-to-right (Card -> AP)
  - Deauth bar "lighting up" during deauth attacks would be cool, highlight the target being deauthed with red background, slowly fades back to normal.
- Separate lines for each client pointing to the router. I don't know if we can capture to/from on client<->AP packets, I feel like this is possible...
- Scanner->Focus transition should slide Focus VIew in from the right.
  - Focus View needs a large obvious "Back to Scanner" button on the upper(?) left side of Focus view, takes user back to scanner (sliding back out).
- Indicate which client we captured the handshake on? Could reuse that green "[check]HS" icon we have in Scanner view.
- Toast notifications for: Handshakes, PIN cracks, PBC, PMKIDs, WEP cracks
  - Red Toast when no beacon seen for X seconds (30 sec?) - OK to repeat 30sec later, reminds/nudges the user.
  - Orange on when deauthing a "PMF: Optional" AP: "Router advertises Protected Management Frames (PMF), deauths likely will not work, try WPA Downgrade"


### ANSI Art Mockup
```
[  Extract PMKID  ]                                            Power: -71dBm
[ WPS Brute Force ]                                            Beacons: x,xxx
[  WPA Downgrade  ]                                            [ Signal Bar ]
                                                            
     \  /      beacon __________________________________<-9s     \  /
    __\/___      data __________________________________<-1s   ___\/__
   / Alfa /    inject ->__________________________________0s  /______/|
  /___o__/     deauth ->________________________________<-1s |____;_;|/
                eapol __________________________________<-0s
  rtl8187l                                                      NETGEAR91
Alfa AWUS036H                                               xx:yy:zz:xx:yy:zz

+--------------------------------+     CLIENTS (2)     PWR    Pkts                         
| Target Locked.                 |   ff:ee:dd:cc:bb:aa -79dBm   10 [ Deauth ]
| starting attack ...            |   aa:bb:cc:dd:ee:ff -80dBm  134 [ Deauth ]
|                                |                       [ Deauth Broadcast ]
|________________________________|
```

OK the art does help show how cool it would look if we had ANSI blocks and proper router / wireless card artwork.
Completely unique UX. Super easy to understand.
I'm not entirely sure how we'll corral the Log and Client Table...
The ansi art design above still doesn't include Encryption, Capture status, current operation (cracking)
We could easily add a new row for IVS/sec, wait I think eapol & ivs swap depending on WPA/WEP.
WPS PIN, PBC, Handshake, PMKID, or Handshake+PMKID Captured!" bold black on green underneath "eapol" row.
Likewise WEP Crack result bold black on green underneath "ivs" row.

I guess there's room underneath the wireless card to indicate what it's doing during WEP ("Replaying" "Chopping" "Cracking")
And for WPA PIN we could show the PIN attempt, %, ETA...

Although the campaigns have a TON of room at the top center, room for a "CAMPAIGN" panel ("Active Attack" or something).

