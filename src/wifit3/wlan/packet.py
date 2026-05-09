"""
TODO: Implement basic 80211 packet parsing.
      POPOs for just the packets that Wifit3 cares about (deauth/disassoc, handshake, beacon, wps, etc).

Packet Parsing (Dropping Scapy)
Since we want to avoid Scapy (UAC popups, heavyweight), we will need a native, lightweight 802.11 parser.
- It doesn't need to parse every protocol in the world—just the offsets for MAC addresses, Frame Types (Management/Data), SSIDs, and EAPOL headers.
- We can build this cleanly using standard Python struct unpacking, which will be infinitely faster than Scapy and completely silent on the UAC front.
- Support serialization (raw bytes -> packet) and deserialization (packet -> raw bytes)
"""