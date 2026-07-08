# MT76x2U / MT7612U

A port of the mt76 kernel driver, generation `mt76x02` (the older sibling of `mt76_connac`;
kernel module `mt76x2u`, vs `mt7921u` for the WiFi-6 sibling). MT7612U silicon: 2T2R, 2.4 + 5 GHz
802.11ac, two-stage firmware (ROM patch + main FW). The dev-machine card is `0e8d:7612` (Alfa
AWUS036ACM), a USB 3.0 device; 15 VID:PIDs are claimed (`constants.py::USB_IDS_MT76X2U`).

## Status

Cold init, two-stage firmware boot, and dual-band monitor RX all work on hardware. A 30-min
dual-band soak (22 channels, 0.25 s hops) ran with no frame-rate sag — active BSSIDs 147→155, 2.4
GHz ~100+ and 5 GHz ~52 steady. ARP replay works first try and handshakes auto-save (after the
L2PAD fix, below). TSSI is the one open hardware question (see Gotchas).

## Gotchas

**Remove the L2 alignment pad BEFORE trimming to MPDU_LEN.** mt76x02 sets `MT_RXINFO_L2PAD` and
inserts 2 bytes between the 802.11 header and the body whenever the header isn't 4-byte aligned —
i.e. every QoS-Data frame (26-byte header), which is what EAPOL and WEP-ARP ride on. The kernel
de-pads *then* trims, and MPDU_LEN counts the de-padded MPDU, so `rx.py::decode_urb` must match that
order. Windowing to MPDU_LEN first drops the last 2 body bytes — it clipped EAPOL M2 `key_data`
(uncrackable handshake) and shrank WEP ARP from 70→68 B (flaky replay). Beacons/mgmt are unaffected
(24-byte header, no L2PAD), which is why scanning always looked healthy.

**TSSI is gated OFF by default** (`driver.py::_tssi_enabled` needs both the EEPROM flag and
`WIFIT3_MT76X2U_TSSI=1`), deviating from the kernel which trusts the EEPROM. The periodic
`tssi_compensate` path is suspected of zeroing TX power on this silicon (observed `tssi_slope=127`,
near max). The `phy.py` port of `mt76x2_phy_tssi_compensate` audited as matching the kernel, so the
root cause is more likely the EEPROM read feeding it, or the monitor-mode `avg_rssi_all=-75`
placeholder. Needs hardware diagnosis before flipping the default back.

**No patch-semaphore wall.** `rom_protect = !is_mt7612(dev)` is false for this silicon, so the
`MT_MCU_SEMAPHORE_03` acquisition is skipped — this is the structural reason MT7612U doesn't hit the
wall that paused MT7921AU.

**The card enumerates as USB mass storage first** (SCSI BBB, EPs 0x81 IN / 0x02 OUT) before exposing
the wireless EP set. On Windows the WinUSB/Zadig binding plus the first open re-enumerates it into
wireless mode automatically (no manual switch). `transport.assert_expected_endpoints()` fails fast
with an actionable error if the wireless EPs are still missing — also the early-detection guard for
whether the mode switch is stable across power cycles, which is unconfirmed.

**Channel switches may need ~2 s of breathing room** — observed against the vendor stack, not yet
replicated against the wifit3 driver, so unconfirmed as a real firmware constraint.

**20 MHz primary only, by design.** `set_channel_20mhz` hardcodes `bw=0` / `ch_group_index=0`; we
deliberately skip the kernel's 40/80 MHz path. This is the project-wide posture, not a capture gap —
everything wifit3 acts on (beacons, auth/assoc, EAPOL, WEP IVs, all legacy-rate attacks) rides the
20 MHz primary; 40/80 MHz only carries HT data payloads wifit3 never needs.

## Orientation

Two-stage firmware lives in `firmware.py`: a ROM patch (`mt7662_rom_patch.bin` → `0x00090000`) then
main FW split into ILM (`0x00080000`) and DLM (`0x00110000`, or `0x00110800` on rev ≥ E3 — this card
is E4). Chunks upload over the bulk-OUT MCU path on EP 0x08, with the dst address split across two
no-payload control transfers per `mt76u_single_wr`. We ship header-stripped bodies (the
linux-firmware headers never appear on the wire) and skip the header-read step.

Register access is one vendor control transfer each, with two virtual-bus marker bits at the top of
the address selecting bRequest: none → MULTI_READ/WRITE (MAC/BB/RF), `BIT(30)` → CFG bus, `BIT(31)`
→ EEPROM read. Encoding: `wValue = addr >> 16`, `wIndex = addr & 0xFFFF`, 4-byte LE payload.

RX decode + monitor filtering: `mac.py::mac_start(monitor=True)` clears the `MT_RX_FILTR_CFG`
unicast/BSSID drop bits so ToDS capture works. Endpoints are assigned positionally in descriptor
order (`mt76u_set_endpoints`): in_ep `0x84`/`0x85`, out_ep `0x04`–`0x09`, with `0x08` the inband-cmd
EP used for FW upload + MCU. Names match the kernel; grep `data_dumps/mt76-source-v6.18/` to
cross-reference.

## Scripts

- `extract_mt7662_fw.py` — splits the bulk-OUT FW chunks out of the cold-boot pcap into `assets/`.
- `scripts/diag/sweep.py` — multi-channel RX soak / longrun stress (used for the 30-min dual-band run).
- `verify_pcap.py` — offline cold-boot byte gate against `captures_mt76x2u/capture-1.pcap`.

## Debug log

### 2026-07-08 — replug default flipped; opt-out now explicit

`LINUX_REPLUG_AFTER_MODPROBE` now defaults **True** (replug-required is the safe default), so this
chip sets `= False` **explicitly** — the 06-27 note's "does not set it" is superseded; the self-cold
reasoning (`force_power_cycle`) stands. In this family only mt7921au is replug-required (`True`);
mt76x0u and mt76x2u self-cold and opt out with `False`.

### 2026-06-27 — Linux bring-up validated; no replug gate needed

First Linux (Kali VM) validation. Unlike its connac siblings (mt76x0u, mt7921au), mt76x2u does
**not** set `LINUX_REPLUG_AFTER_MODPROBE` and should not: those two can't cold-reset in userland
(replug-only), but `power.force_power_cycle` clears this chip's WLAN block to a cold-equivalent
state without a physical replug, so the device-setup no-replug path self-recovers a kernel-warmed
chip. Confirmed end-to-end: install-rules→boot, warm reboot, and unplug/replug all succeed; passive
`test_hw_mt76x2u --phase rx` (27 BSSIDs ch6) and `--phase hop` (107 BSSIDs, 65×2.4 GHz + 42×5 GHz)
both PASS. Note: `test_hw_mt76x2u`'s default/`all` phase fires live deauth — use `--phase rx|hop`
for passive validation.

### 2026-05-29 — L2PAD clipped EAPOL/WEP

RX was healthy for scanning but handshakes were uncrackable and WEP ARP replay flaky. Root cause was
the L2PAD ordering (now in Gotchas): windowing to MPDU_LEN before de-padding dropped the trailing 2
body bytes, but only on QoS-Data frames — beacons/mgmt have a 4-byte-aligned header and never set
L2PAD, so scanning masked it. Fixing the de-pad-then-trim order made ARP replay work first try and
handshakes auto-save.

### 2026-06-20 — firmware provenance

The pcap-extracted bodies are byte-identical to linux-firmware `mediatek/mt7662.bin` +
`mediatek/mt7662_rom_patch.bin` (not the `mt7662u*` variants): the ILM slice, trailing DLM, and
rom-patch body all match, and mainline `mt76x2u` requests exactly these. WHENCE files them under
driver `mt76x2e` → governed by `LICENCE.ralink_a_mediatek_company_firmware`, which ships alongside
the blobs in `assets/`. The DLM landing at `0x110800` plus an ASIC version of `0x76120044` pins the
silicon at rev E4.
