# RTL8821AU

Realtek 802.11ac single-stream USB chipset (rtw88 family), ported from `rtw88-source-v6.18`
against `capture-1.pcap`. The driver is shared with RTL8811AU — both map to `rtw8821a_hw_spec`
in the kernel.

## Status

Cold init, firmware upload + run, MAC/BB/RF init, 2.4 + 5 GHz channel tune, and monitor RX all
work on hardware (ALFA AWUS036ACS, `0bda:0811`, USB 2.0 high-speed only). Passive handshake/PMKID
capture in Focus is reliable. Full M1–M4 4-way is captured once the monitor RX filter is in place
(see Gotchas). Not done/verified: warm reattach. TX (deauth/inject) is wired but not agent-tested.

## Gotchas

**Monitor RX needs the airmon RCR, or you never see the full 4-way.** STA-mode's default RCR isn't
promiscuous, so only the FromDS halves (M1/M3) arrive and there's no handshake. Writing the exact
airmon monitor value `0xf410400f` (AAP set, CBSSID cleared) in `apply_monitor_rx_filter` — called
from `_finish_attach` so it runs on both cold and warm attach — fixes it. Net-type is *not* the
gate: the kernel keeps net-type at `MGD_LINKED(2)` in monitor mode.

**RX must run on a dedicated reader thread.** On-loop read+parse drops ~30% of beacons and catches
only ~1-in-5 4-way handshakes, because the 10 Hz UI render starves the read loop and the dongle
FIFO overflows with no URB posted. The reader thread keeps a read posted always and hands buffers
to the loop via `call_soon_threadsafe`.

**RF init runs at rfe=0.** The EFUSE-derived `rfe` value selects IF/ELIF/ELSE branches in the agc
and rf_a tables; until that's read we fall through to the ELSE branches with rfe=0. The branches
depend on `(intf, rfe)` only, never `cut`.

**Skipped, empirically not needed for beacon RX:** `rtw_phy_init` (DIG) and `pwrtrack_init`. If RX
sensitivity ever looks off, these are the first suspects.

The post-FW-upload `REG_MCUFW_CTRL` carries chip-status bits `0x300` (bits 8,9) that aren't named
in upstream `reg.h` — expected, not a divergence.

## Orientation

RTL8821A: 1T1R, 2.4 + 5 GHz 802.11ac, legacy 8051 wlan CPU, single RF chain (`rf_a_tbl` only).
Firmware is a 31898-byte asset with a 32-byte `rtw_fw_hdr_legacy` prefix; the prefix is metadata
stripped before upload and the body is byte-identical to what goes on the wire. Upload success is
the device setting `BIT_FWDL_CHK_RPT` in `REG_MCUFW_CTRL`.

MAC init is `power_on` (pre-FW FIFO/LLT + post-FW queue/wmac/edca/ARFR). PHY init in `phy.py`
loads mac/bb/agc/rf_a tables then runs `switch_band`. Channel tune is `chan.set_channel` (RF
read-modify-write via SIPI path A). RX decode + reader thread live in `rx.py` / `driver.py`. The
phy-cond walker (`phy_cond.py`) mirrors `rtw_parse_tbl_phy_cond`. Names match the vendor C — grep
`driver_sources/rtw88-source-v6.18/` to cross-reference.

The EFUSE read (512× write32 to `REG_EFUSE_CTRL`) happens at pcap frames 936–3122 — this is where
`cut`, `rfe_option`, btcoex, and ext_lna/pa flags come from; it is *not* LLT.

## Scripts

- `extract_rtl8821au_fw.py --verify` — confirms the asset body byte-matches the wire upload.
- `extract_init_tables.py` — emits the flat-u32 mac/agc/bb/rf_a tables under `assets/`.

## Debug log

### 2026-05-25 — full 4-way capture

Two cross-driver gap classes hit this card. (1) On-loop RX read+parse, starved by the TUI, dropped
~30% of beacons and most handshakes; a dedicated reader thread fixed it (commit 2e3a7a7). (2) Only
M1/M3 (FromDS) were ever seen — the STA RCR isn't promiscuous. Writing the airmon RCR `0xf410400f`
from `_finish_attach` got full M1–M4 (commits 24bc17d, b6e7cb9). Net-type was a red herring: the
pcap shows the kernel leaves net-type at `MGD_LINKED(2)` in monitor.

### 2026-05-17 — cold bring-up to RX, on hardware

FW upload (~320 control transfers, ~31 ms; faster than the budgeted 1 ms/transfer), FW run
(`download_firmware_validate_legacy`), MAC init (4.5 ms), full PHY init (360.9 ms), RX (27 BSSIDs
in 8 s, 803/803 frames parsed), and channel tune (5.5 ms for ch1) all passed first try. `rtw_phy_init`
(DIG) and `pwrtrack_init` were skipped and beacon RX still worked.
