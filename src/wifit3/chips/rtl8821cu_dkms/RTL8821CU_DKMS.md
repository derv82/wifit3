# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status — milestone 1 prologue GREEN.** The byte-for-byte gate
> (`scripts/rtl8821cu_dkms/verify_pcap.py`) reproduces the cold-boot wire for **2068 control
> ops, zero divergence**: USB transport (+ the 8821c `0x4E0` mirror), the halmac mount
> chip-detect, the chip-version read, and the full 512-byte EFUSE dump. The HALMAC card-enable
> power tables are also ported (they verify once the short pre-power init block lands — the
> current frontier). Not registered in `wlan/manager.py` (claims nothing until complete).

## Silicon

| | | |
|---|---|---|
| USB ID | `0bda:c820` | [WIRE] capture; [SRC] usb_intf.c:263 |
| Silicon | RTL8821C, 1T1R, 2.4 + 5 GHz 802.11ac | vendor tree name |
| MAC/PHY family | HALMAC + PHYDM (same infra as 8822b/8822c) | [SRC] hal/halmac/halmac_88xx/halmac_8821c/ |
| Firmware | yes — `array_mp_8821c_fw_nic[]` (~139 KB) | [SRC] hal/rtl8821c/hal8821c_fw.c (doors-map; confirm at FW milestone) |
| Chip ver / cut | `SYS_CFG1` (0xF0) = `0x00494537` on this card | [WIRE] f546 |

## Entry points (the doors → our `.py`)

| phase | our `.py` | vendor `.c` | note |
|---|---|---|---|
| USB probe / id_table | `driver.SUPPORTED_IDS` | os_dep/linux/usb_intf.c:142 (VID), :263 (`0xC820`, `.driver_info = RTL8821C`) | verified VID; id line from doors-map |
| register transport | `transport.Rtl8821cuTransport` | include/usb_ops.h:19-22,30 ; os_dep/linux/usb_ops_linux.c:26-260 | bRequest 0x05, 0xC0/0x40 |
| ON-section mirror | `transport._mirror` | os_dep/linux/usb_ops_linux.c:171-201 (`t_reg = 0x4e0` :191) | gated on `CONFIG_RTL8821C` |
| mount chip-detect | `chipid.mount_get_chip_info` | hal/halmac/halmac_api.c:492 get_chip_info (USB :518-520) | SYS_CFG2 0xFC + SYS_CFG1+1 0xF1 — **VERIFIED** |
| chip-version read | `chipid.read_chip_version` | hal/rtl8821c/rtl8821c_ops.c:34 | SYS_CFG1/STATUS1/0x68 — **VERIFIED** |
| EFUSE dump | `efuse.read_efuse` / `read_hw_efuse` | hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1088 + rtl8821c_ops.c:462 | 512 B via the 0x30 indirect loop — **VERIFIED** |
| pre-power init | — **(frontier)** | `pre_init_system_cfg_8821c` halmac_init_8821c.c:975 ; `mac_pwr_switch_usb_8821c` preamble halmac_usb_8821c.c:32 (:44-61) | ops 2068-2105: RSV_CTRL/PAD_CTRL1/LED_CFG/GPIO_MUXCFG/SYS_FUNC_EN/RF_CTRL/WLRF1 rmw, then rpwm 0xFE58 / MCUFW 0x80 / CR 0x100 / SYS_STATUS1+1 reads |
| power on/off | `pwrseq` (CARD_EN_FLOW) | hal/halmac/halmac_88xx/halmac_8821c/halmac_pwr_seq_8821c.c:20-349 | 4 tables transcribed 1:1; verifies once pre-power lands |
| pwr-seq runtime | `pwrseq.run_pwr_seq` / `_run_table` | hal/halmac/halmac_88xx/halmac_common_88xx.c:2980 / :3051 | doors-map; confirm at next M |
| firmware download | — | hal/hal_halmac.c:3350 `download_fw` ; hal/rtl8821c/rtl8821c_halinit.c:149 | doors-map; later milestone |
| MAC/BB/RF init | — | hal/rtl8821c/usb/rtl8821cu_halinit.c:55 → rtl8821c_halinit.c:264 | doors-map; later milestone |

## Hot paths

- `transport._mirror` (`transport.py`) — after every ON-section vendor access (addr ≤ 0xFF or
  0x1000–0x10FF), a 1-byte write to `0x4E0` of the IO-buffer low byte. Verified byte-for-byte
  against the wire. [SRC] usb_ops_linux.c:171-201.
- `pwrseq._run_table` (`pwrseq.py`) — HALMAC `pwr_sub_seq_parser`: WRITE = read-modify-write,
  POLLING = read-until-masked-match, DELAY/READ = no-op, the USB intf filter drops SDIO/PCI rows.

## Scripts

- `scripts/rtl8821cu_dkms/verify_pcap.py` — the byte-diff gate vs the cold-boot pcap
  (`usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`, the cold-boot of the 4 captures). Run:
  `uv run python scripts/verify_pcap.py rtl8821cu_dkms`. It drives `bringup.cold_bringup` against
  the recorded wire; a clean run prints `reproduced N/… ops clean` and a `FRONTIER ->` line naming
  the next op to port. Add ops-dump probes inline (see this session's frontier dumps) to read a
  byte range. Do NOT edit the gate to pass — port the diverging op (PORTING.md Step 3).

## Caveats

- The 8821c power tables differ from 8822b's (no `0xFF0A/0xFF0B/0x0012` LDO rows, different PCI
  block, no cut-C `0x10A8` rows, ACT ends at `0x007C`). This is why the port is self-contained,
  not a reuse of `rtl8822bu_dkms`.
- Every 8821c card_en/dis row is `CUT_ALL`, so the chip cut does not filter the power sequence;
  the real cut (from `SYS_CFG1` 0xF0) only gates the init tables that follow.
- **ZeroCD / mode-switch (hardware only).** The card ships in a USB CD-ROM ("ZeroCD") mass-storage
  mode and must be mode-switched to the WiFi function (PID `0bda:c820`) before the driver binds;
  on Windows it also exposes a CD-ROM LUN alongside the NIC. The cold-boot pcap was captured
  already in WiFi mode (the kernel / usb_modeswitch flips it at plug-in), so the **offline verify
  is unaffected** — but the runtime driver (a later milestone) must issue the mode-switch (SCSI
  eject / usb_modeswitch payload) when it sees the CD-ROM PID, before WinUSB/Zadig binding. Not on
  the verify path.

## Known issues

- **Frontier (next milestone): the pre-power-on init block, wire ops 2068-2105.** Two doors:
  `pre_init_system_cfg_8821c` ([SRC] halmac_init_8821c.c:975) — rmw of RSV_CTRL (0x1C=0),
  PAD_CTRL1 (0x64), LED_CFG (0x4C), GPIO_MUXCFG (0x40), and the SYS_FUNC_EN (0x02) / RF_CTRL
  (0x1F) / WLRF1 (0xEC) enable writes; then the head of `mac_pwr_switch_usb_8821c`
  ([SRC] halmac_usb_8821c.c:32, :44-61) — reads rpwm (0xFE58), MCUFW_CTRL (0x80), CR (0x100),
  SYS_STATUS1+1 (0xF5) to decide power state — which then calls `run_pwr_seq(card_en)` at op
  #2106 (the already-ported `pwrseq.CARD_EN_FLOW`). Port the two doors above; `pwrseq` verifies
  in behind them. (Gate-driven: confirm the exact 0x02/0x1F/0xEC attribution as each op lands.
  Note `0xFE58` is LOCAL-section ≥ 0xFE00 → NO `0x4E0` mirror; the transport handles that.)
- `transport` bulk-OUT EP defaulted to `0x04` (not on the prologue path) — confirm against the
  coverage audit (`bulk-OUT ep 0x05`) at the FW/TX milestone. The audit also flags interrupt-IN
  ep `0x81` (360 pkts) as a blind spot to check at the FW/C2H milestone.

## Port log — 2026-06-22 (scaffold + M1 power tables)

- Built the self-contained dir: `transport.py` (+0x4E0 mirror), `pwrseq.py` (4 × 8821c tables
  verbatim), `bringup.py`, `driver.py` (WIP skeleton), `constants.py`. Added
  `scripts/rtl8821cu_dkms/verify_pcap.py` on the shared Realtek `rtw88_pcap_replay` relay and
  registered the chip in the top-level `scripts/verify_pcap.py`.
- Gate result: transport + `0x4E0` mirror reproduce the wire byte-for-byte (the recorded
  `IN <reg>` / `OUT 0x4e0=<lowbyte>` pairs match). Divergence at op #0 identified the real M1 as
  the chip-id/pre-init prologue above — power-on is M2. Doors-map (Explore subagent) had guessed
  power-on as first; the pcap corrected it (PORTING.md: the pcap is the map).
- Next session: port the chip-id/pre-init prologue, then re-run the gate — power tables should
  fall in behind it cleanly.

## Port log — 2026-06-22 (M1 chip-id + EFUSE prologue GREEN)

- Ported the prologue the gate had pointed at, all from source: `chipid.mount_get_chip_info`
  (halmac_api.c:492 get_chip_info — SYS_CFG2/SYS_CFG1+1), `chipid.read_chip_version`
  (rtl8821c_ops.c:34), and `efuse.read_efuse` (rtl8821c_ops.c:462 + halmac_efuse_88xx.c:1088
  read_hw_efuse — the 512-byte `0x30` indirect loop; bank-switch + cfg_ldo25 setup).
- Gate now reproduces **2068/20833 control ops with zero divergence** — the whole chip-id +
  EFUSE prologue is byte-for-byte. Discovery loop worked exactly as PORTING.md describes: each
  gate divergence named the next register block, traced to its C function, ported, re-ran.
- New frontier at op #2068: the pre-power-on init block (see Known issues), then `0x4A`
  power-on at op #2106. `power_on` removed from `cold_bringup` for now (not wire-adjacent to
  EFUSE); the power tables stay in `pwrseq`, ready to verify once the pre-power block lands.
