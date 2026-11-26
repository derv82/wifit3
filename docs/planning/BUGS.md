# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## (CANTFIX) RTL8822BU TP-Link Archer T4U v3 / T4U+ ambiguity

**Problem.** TP-Link manufactured multiple different models that all share
the same exact device metadata.  A USB device with VID:PID `2357:0115` could be
Archer T4U v3, v3.2, or Archer T4U+. All other descriptors appear to be identical.

Known T4U+ sample (identical to T4U "v3.6" sample):
- `idVendor:idProduct`: `2357:0115`
- `manufacturer`: `Realtek`
- `product`: `802.11ac NIC`
- `serial`: `123456`
- `bcdDevice`: `0210`
- `bcdUSB/version`: `2.10`
- `speed`: `480`
- `chipset`: `RTL8822BU` (could be RTL8812BU given online reports)

Links:
- [linux-hardware.org@`2357:0115`](https://linux-hardware.org/?id=usb:2357-0115) (RTL8822BU, RTL8812BU)
- [Wi-Cat.ru@T4Uv3.2](https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3.2) *("probably rtl8812bu")*
  - [Wi-Cat.ru `2357:0115` disambiguation](https://shorturl.at/5iTC7)

**Direction.** Keep dumping device info from other models; spot the diff.

## Expand EFUSE support

Most drivers were ported against the single device they were tested on, so EFUSE derived values
(antenna count, TX power tables) sit in the code as constants. A device whose EFUSE differs then
gets wrong values with no error raised: little or no RX/TX, or wedging.

Chipsets confirmed to honor EFUSE: RTL8821AU, RTL8822BU, RTL8822CU, RTL8192FU, AR9271.
RTL8192FU is verified only on its RFE type 5 reference card; RFE type 1 needs hardware validation.

Direction: per driver, list the EFUSE fields the vendor driver reads, compare against the wifit3
driver, port what is missing. The vendor's per field parsers are uniformly named, so the field list
comes out of the vendored source directly:

    grep -rhoE 'Hal_EfuseParse[A-Za-z0-9_]+' <vendor-tree>/ | sort -u

On a tree that compiles several chips that reports more fields than the one chip uses. Narrowing it
to the chip's own efuse reader gives the exact set, e.g. for RTL8822CU:

    sed -n '/rtl8822c_read_efuse/,/^}/p' hal/rtl8822c/rtl8822c_ops.c | grep -oE 'Hal_[A-Za-z0-9_]+' | sort -u

## PixieDust glibc seeding is modelled as unsigned

**Problem.** `pixie_prng.glibc_nonce` keeps the seed unsigned, so it diverges from glibc for seeds
>= 2^31: `glibc_nonce(2147483648)` yields `2b101346...`. Unix timestamps cross 2^31 in **January
2038**, after which RTL819x recovery silently stops working. Also reachable today only via a
caller-supplied `rtl_window` low enough that `nonce_seed - dist` goes negative.

**Direction.** Establish what glibc actually does first: `__srandom_r` assigns the seed to an
`int32_t` state but runs Schrage's step through a `long int`, so the answer differs between 32- and
64-bit builds. Settle it against a real glibc on both, then match it and add a known-answer vector
above 2^31. Clamp or reject negative seeds either way.

## PixieDust eCos mode mishandles a high nonce byte

**Problem.** `ecos_simple_recover` computes the known seed bits as `(e_nonce[0] << 25) & 0xffffffff`,
which discards bit 7 of the byte. Any enrollee whose nonce starts >= 0x80 makes it sweep the wrong
2^25 space and return `None`. `ecos_simple_model` only ever emits `nonce[0] < 0x80`, so the round
trip is self-consistent and the tests cannot see it. `ECOS_SIMPLE` is experimental and not in
`DEFAULT_MODES`, so nothing ships broken today.

**Direction.** Work out what the device really does with the top bit — whether the nonce byte is
the seed's top 7 bits or its top 8 with one bit lost to the shift — before switching the mode on.

## PSK-SHA384 and FT-PSK-SHA384 handshakes are not yet supported

**Problem.** AKM 20 (PSK-SHA384) and AKM 19 (FT-PSK-SHA384) derive the KCK and MIC with SHA-384
(24-byte MIC). `-m 22000` has no SHA-384 path in released hashcat, so wifit3 withholds these
captures as "not yet supported". Hashcat mainline added one ([hashcat#4931](https://github.com/hashcat/hashcat/issues/4931)).

**Direction.** Once a hashcat release ships it, add the hashline format and drop the withhold.

## WPA-Enterprise credentials are not yet supported

**Problem.** An Enterprise 4-way is keyed by a per-session PMK from EAP and is never crackable.
The crackable secret is the user's credential: an EAP-MD5 challenge/response (`-m 4800`) sent in
the clear, or an MSCHAPv2 response (`-m 5500`) that PEAP/TTLS hide inside TLS, so it takes a rogue
AP (an Enterprise EvilTwin). Vault, the cracker and capture storage all assume `.hc22000`.

**Direction.** A new capture type through Vault and the cracker; scope it before starting.

## Most drivers have no TX lock

**Problem.** Without a lock, concurrent injects reach the device out of call order. Before mt7921au
got one, the TX lab saw 42-46 of 500 flooded frames arrive out of order (rtl8822bu has a lock: 0).
No lock in: mt76x0u, mt76x2u, mt7925au, rt3572, rt5572, rtl8187, rtl8922au.
rt3572 / rt5572 can also transmit in the middle of a retune.

**Direction.** A lock per driver, written in each driver (no shared base); re-run the TX lab flood
test on each.

## Focus view CPU usage

**Problem.** Focus is the most CPU-heavy screen. After the LED art and signal bar were changed to
repaint only on change, 120 s of Focus costs 27.5-30.7 s of CPU on the Windows PC (was 44.8 s),
against 24.8 s for the Scanner (1 run each). Remaining suspects are the per-tick snapshot rebuild
and Textual compositing.

**Direction.** Cache raw values and rebuild markup only when a value changes, instead of
rebuilding the whole snapshot each tick.

## `beacon_clone` copies HE/EHT operation widths as-is

**Problem.** `beacon_clone` forces the HT/VHT operation elements to 20 MHz but copies HE and EHT
operation as-is, so a 5 GHz target that advertises a wider channel gets a twin that does too.

## 40/80 MHz APs may be invisible to the scanner

**Problem.** An AP that defaults to 40 MHz is not seen. 40/80 MHz was never ported because it was
unclear how scanning should use those widths. Unconfirmed as a bug: the wifit3-vs-Linux tests
showed about the same AP counts.

**Direction.** Reproduce with the 40 MHz AP, then decide whether scanning needs wide-channel support.

## MT76, Ralink and RTL8187L resend unicast that was already ACKed

These cards stop resending only when the ACK is for their active-monitor MAC (RTL8187L: its silicon
MAC). Any other source keeps resending to the retry limit even though every copy is ACKed: 7-16
copies per frame against a live AP (`docs/ACKS.md`, "HW ACK-Based Retries"). Hits every unicast sent
without active monitor, e.g. `deauth_client` spoofing the AP and the client.

Measured: mt7601u, mt76x0u, mt76x2u, mt7921au, mt7925au, rt3070, rt5370, rt5372, rt5572, rtl8187.
Unmeasured, same vendor families: rt3572, rt2800usb.

Fix: when the frame's source isn't the active-monitor MAC, inject it with no ACK requested (sent
once; `send_until_ack` retries in software), like the broadcast fix in `3201c92e`.

## RTL8821CU never reports its silicon MAC

`rtl8821cu_dkms` reads the MAC from EFUSE and writes it to the chip during bring-up
(`bringup.py:124`, `driver.py:307`), but `self.mac_address` stays at its initial `None`
(`driver.py:75`): nothing assigns it. So `WlanInterface.mac_address` is None for every 8821CU: the
Focus card shows no MAC, and `scripts/ack/rx_autoack.py` blames a warm boot.

Fix: assign `self.mac_address` from `efuse.mac_address(self.info)` at bring-up, like the other drivers.

## Vault: Race condition when writing `.meta`; raises or loses fields

`update_meta` (`persist/meta.py`) reads the `.meta`, merges the fields and writes it back through one
fixed `<name>.meta.tmp`, with no lock. Two writers at once raise `OSError` on the replace (Windows
`PermissionError`, Linux `FileNotFoundError`), or the last writer drops the other's fields. Real path:
a crack job's PSK propagation thread and the open Vault drawer's reload write the same related AP.
A two-thread repro (300 writes each) lost most keys on Windows 3.13 and Linux 3.11 / 3.14. Likely
cause of one failure of `test_add_key_adds_a_psk_no_saved_capture_confirms_as_not_verified` (1 in 38
full-suite runs, Kali, 3.14).

Fix: a unique temp file per write (`tempfile.mkstemp`), plus a lock around read-merge-write. Callers
that read the `.meta` before `update_meta` need the lock too, or `update_meta` takes a merge function.
11 write call sites.

## MT7612U warm bring-up wedges until a replug

**Problem.** After several warm bring-ups, `connect()` fails with `BringUpError(mcu-load-cr): LOAD_CR
(BBP table) failed after a power cycle + cold reset` (`MCU CMD cmd=2 seq=1 response timeout`), and
every later bring-up fails until the card is replugged. 1 card, 2 runs: the first failure came on
warm bring-up #12 and #7.

## RTL8192FU reopen in the same process is denied

**Problem.** `scripts/bringup.py --chipset 8192 -n 10` brings the card up once; cycles 2-10 fail
with `BringUpError: [Errno 13] Access denied`. A new process opens it fine. 1 card, 2 runs, Windows.

## MAC filter and ACK delivery differ across drivers

**Problem.** Some drivers pass ACK frames up by default, others filter them; `enable_rx_acks`
counts only ACKs addressed to us. Whether active monitor (auto-ACK, retry-until-ACK) works per
driver may depend on the same MAC filter bits. Not surveyed.

**Direction.** Per driver: does RX deliver every ACK on the air, with and without `enable_rx_acks`.

## WEP campaigns keep sending after the device is gone

**Problem.** Once a driver reports the device gone (ENODEV), ARP replay retries a burst every
2 s until the user leaves Focus (`arp_replay.py:294` catches and logs), and Focus shows nothing.
Seen once, RTL8814AU on a loose cable, Windows: 9 failed bursts over 18 s; RX kept working.
