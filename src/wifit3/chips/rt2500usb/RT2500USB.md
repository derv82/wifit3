# RT2500USB (Ralink RT2570)

Userland PyUSB port of the Linux `rt2500usb` kernel module. RT2570 is a full-speed (USB 1.1-class)
802.11b/g hard-MAC radio with **no firmware** — all bring-up is register pokes over USB control
transfers. This unit is a Buffalo "Nintendo Wi-Fi USB Connector" (`0x0411:0x008b`) carrying an
RF2525E synthesizer.

## Status

Full passive + active stack working on hardware: scan, monitor, 14-channel hop, deauth, WPS
(PBC → PSK; PIN → M4), WEP ChopChop + ARP replay, PMKID. `verify_pcap` reproduces 100% of the
control conversation across the captures with zero waivers. A clean 30-min hopping soak held with
no wedge after the stability hardening below; the older grade-D "one AP 0/s, RF dies after a minute"
behavior is gone.

## Gotchas

**This is a weak, older radio.** RX sensitivity/stability is marginal on this hard-MAC part — judge
RX against a strong nearby reference AP, not absolute beacon counts. The weak-RX era kept losing
ToDS frames (M2/M4 of a handshake); ToDS reception itself works (confirmed on the wire), so weak RX
was the cause, not a filter gap.

**Missing `reset_tuner` was the weak-RX root cause.** rt2x00 calls it on every channel change and
antenna config — unconditionally, monitor mode included — to seed the AGC (BBP R17/R24/R25/R61 from
EEPROM BBP-tune words). The original port never ran it, leaving BBP R17 stuck at its init 0x30 (this
unit wants 0x3b) and never re-seeded per hop. Now in `bbp.reset_tuner`, wired into `monitor.tune_hop`.

**The headline bug was an RF-death wedge** under sustained load — the bulk-IN pipe stalls after
~1 min, then control ops fail with `Errno 32 Pipe error` (a full-speed control op racing the bulk-IN
reader fast-fails). Hardened, not eliminated: `transport._ctrl` does a bounded transient retry, and
`driver` carries the rt3070 `_io_lock`/`_hw_lock` pair so a cancelled tune's draining thread can't
collide with a new tune/inject on the control endpoint.

**The monitor filter drops PLCP + CRC errors on purpose, and that matches the kernel wire** — final
`TXRX_CSR2 = 0x0046`, the same value airmon writes. Surfacing PLCP/FCS failures floods this
full-speed bus (a `--phase rx` drain saw 93% of URBs as multi-KB PLCP noise; another ~45% were
single corrupt FCS-fail frames, not coalesced transfers), and the RX loop drops them in software
anyway — so dropping them in hardware is pure bus savings with zero output change. `monitoring=True`
clears DROP_NOT_TO_ME + DROP_TODS so client→AP frames arrive.

**RXD trails the frame; TXD leads it.** Bulk-IN on EP 0x81 is `[802.11 frame][pad][RXD 16B]` — the
descriptor is at `buf[-16:]`, not the front. Bulk-OUT on EP 0x01 is `[TXD 20B][frame, no FCS]`; the
chip appends FCS and assigns the sequence number.

**CSRs are 16-bit** (vs rt2800's 32-bit), address in `wIndex` with `wValue=0`. BBP/RF are indirect
via PHY_CSR7/8 (BBP) and PHY_CSR9/10 (RF, write-only, no read path). EEPROM is a one-shot stream of
all 110 bytes from byte 0. No `config_intf`: a monitor vif programs no MAC/BSSID/beacon-sync.

## Orientation

Cold bring-up is `mac.py` (`init_registers` + `init_bbp` + the MAC_CSR17 set_state(AWAKE)
handshake). Channel tune + RF2525E/RF2525 tables are in `chan.py`; the AGC seed is `bbp.reset_tuner`.
Monitor RX filter is `mac.config_filter`. RX decode (RXD-at-end) is `rx.py`; TX-desc build is `tx.py`.
`monitor.py` holds the rt2x00 call order (`enable_monitor` + `tune_hop`), shared by the driver and
the verify gate. Names match the kernel C — grep `driver_sources/rt2x00-source-v6.18/rt2500usb.c`.

## Scripts

- `scripts/chips/rt2500usb/verify_pcap.py` — single monotonic cursor over init → monitor entry → every hop.
- `scripts/chips/rt2500usb/test_hw_rt2500usb.py --phase {open,macinit,bbpinit,chaninit,rx}` — incremental bring-up; `--phase rx` drains the bulk-IN pipe for the PLCP/FCS-noise diagnostics above.

## Debug log

### 2026-07-11 — RF-chip generalization (runtime EEPROM, not reference-locked)

`chan.config_channel` used to carry only the RF2525/RF2525E rf_vals tables and
raise `NotImplementedError` for any other RF chip. Ported the four missing
kernel tables (RF2522/RF2523/RF2524 + RF5222's 2.4 GHz rows) so the driver tunes
whatever `EEPROM_ANTENNA_RF_TYPE` the card reports; an RF value outside the six
now falls back to RF2525 with a one-shot "untested variant" log instead of
crashing. The reference unit (RF2525E) path is byte-identical — half-band
pre-tune stays RF2525E-only, and the pcap gate still reproduces cap-1/cap-2 with
zero waivers. The antenna path (`config_ant` TX I/Q flip, `antenna_defaults`
SW→HW) and the BBP/RSSI tune words were already runtime-EEPROM-driven; only the
RF table switch was reference-locked. `EEPROM_NIC` HardwareRadio/DynamicTxAgc/LED
flags gate no register write in the RX/TX/monitor path (LED-trigger + rfkill
only) — nothing to generalize there.

### 2026-06-11 — operational re-port + RX AGC fix

The original port only reproduced `init_registers` + `init_bbp` (123 of 3215 control ops);
everything operational — antenna, channel tune, the AGC seed, the LED — was unverified, and the AGC
seed (`reset_tuner`) was missing entirely, which was the weak/inconsistent-RX root cause. Re-ported
the full operational sequence and made `verify_pcap` one monotonic walk over the whole capture
(now 100%, zero waivers). Same pass added the stability hardening (`_io_lock`/`_hw_lock` + transport
bounded-retry) for the ~1-min RF-death wedge. HW after: best AP ~9 beacons/s, 0 dead seconds, 10+
APs on CH1; 30-min 14-channel soak held 40-58 BSSIDs with no wedge, connected WARM and sustained.
Full attack pass (hands-on): deauth → EAPOL M1+M2+M3 (M2 is the ToDS message the weak-RX era lost);
WPS PBC → PSK; ChopChop forged a packet; ARP replay ~60 IVs/s (burst-batched on purpose so the radio
RX's replies instead of self-DoSing — that rate is the full-speed-bus ceiling, not a port limit).

### 2026-05-31 — ToDS RX confirmed; instability is the real problem

A full attack pass showed `[RXFRAME] data to_ds=True` lines, proving client→AP frames are delivered
— so the lost M2/M4 of handshakes was weak/unstable RX, not a filter gap. The run still ended badly:
RF died after ~1 min under load (bulk-IN pipe wedges, repeating `Errno 32` on read + set_channel),
one CH1 AP gave ~10 beacons/s while another gave 0/s, ARP replay only ~1-3 IVs/s, ChopChop stalled.
All pointed at RX sensitivity/stability on this old part. The `--phase rx` drains that drove the
filter decision are folded into the monitor-filter gotcha above.
