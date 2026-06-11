"""
World ('00') regulatory-domain channel table for the MT7921AU.

mt76_connac_mcu_set_channel_domain walks the registered channel list and emits,
for each *enabled* channel, ``{ __le16 hw_value; __le16 pad; __le32 flags; }``,
where ``flags`` is cfg80211's per-channel IEEE80211_CHAN_* bitmask after the
regulatory domain is applied.

With no userland regulatory subsystem we announce the same world ('00') domain
the kernel defaults to when no country is set. cfg80211 (v6.18) produces exactly
the flags below — a *universal* regulatory constant: the 324-byte channel-domain
payload is byte-identical across every capture and both physical units (pau0f and
AXML), so this is the same class of fixed external constant as the channel
frequencies, NOT a per-card value to read from hardware.

Flag bits (uapi/linux/nl80211.h IEEE80211_CHAN_*):
  NO_IR=0x0002  RADAR=0x0008  NO_HT40PLUS=0x0010  NO_HT40MINUS=0x0020
  NO_OFDM=0x0040  NO_80MHZ=0x0080  NO_160MHZ=0x0100  NO_320MHZ=0x80000
The world domain marks 2.4 GHz ch12-14 and all 5 GHz channels NO_IR (passive),
the 5 GHz DFS band (ch52-144) RADAR, ch14 NO_OFDM; every channel is NO_320MHZ.
"""

WORLD_ALPHA2 = b"00\x00\x00"
# channel-domain header bandwidth fields (mt76 defaults): BW_20M for 2.4 GHz,
# BW_20_40_80_160M for 5/6 GHz.
WORLD_BW_2G = 0
WORLD_BW_5G = 3
WORLD_BW_6G = 3

# (hw_value, flags) — exactly cfg80211's world-domain output for the mt7921's
# registered channel list. No 6 GHz channels are enabled in the world domain.
CHANNELS_2GHZ = [
    (1, 0x000801A0), (2, 0x000801A0), (3, 0x000801A0), (4, 0x000801A0),
    (5, 0x00080180), (6, 0x00080180), (7, 0x00080180), (8, 0x00080180),
    (9, 0x00080180), (10, 0x00080190), (11, 0x00080190), (12, 0x00080112),
    (13, 0x00080112), (14, 0x000801F2),
]
CHANNELS_5GHZ = [
    (36, 0x00080020), (40, 0x00080002), (44, 0x00080000), (48, 0x00080002),
    (52, 0x0008000A), (56, 0x0008000A), (60, 0x0008000A), (64, 0x0008001A),
    (100, 0x0008002A), (104, 0x0008000A), (108, 0x0008000A), (112, 0x0008000A),
    (116, 0x0008000A), (120, 0x0008000A), (124, 0x0008000A), (128, 0x0008000A),
    (132, 0x0008000A), (136, 0x0008000A), (140, 0x0008000A), (144, 0x0008001A),
    (149, 0x00080120), (153, 0x00080100), (157, 0x00080100), (161, 0x00080102),
    (165, 0x00080112),
]
