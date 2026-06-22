# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status — milestone 1, WIP.** USB transport (+ the 8821c `0x4E0` ON-section mirror) and
> the HALMAC card-enable power tables are ported. The byte-for-byte gate
> (`scripts/rtl8821cu_dkms/verify_pcap.py`) runs and confirms the mirror reproduces the wire.
> Not registered in `wlan/manager.py` (claims nothing until functionally complete).

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
| power on/off | `pwrseq` (CARD_EN_FLOW) | hal/halmac/halmac_88xx/halmac_8821c/halmac_pwr_seq_8821c.c:20-349 | 4 tables transcribed 1:1 |
| pwr-seq runtime | `pwrseq.run_pwr_seq` / `_run_table` | hal/halmac/halmac_88xx/halmac_common_88xx.c:2980 / :3051 | doors-map; confirm at M2 |
| chip-id / pre-init | — **(M1 frontier, unported)** | read_chip_version path (SYS_CFG1 0xF0) | see Known issues |
| firmware download | — | hal/hal_halmac.c:3350 `download_fw` ; hal/rtl8821c/rtl8821c_halinit.c:149 | doors-map; later milestone |
| MAC/BB/RF init | — | hal/rtl8821c/usb/rtl8821cu_halinit.c:55 → rtl8821c_halinit.c:264 | doors-map; later milestone |

## Hot paths

- `transport._mirror` (`transport.py`) — after every ON-section vendor access (addr ≤ 0xFF or
  0x1000–0x10FF), a 1-byte write to `0x4E0` of the IO-buffer low byte. Verified byte-for-byte
  against the wire. [SRC] usb_ops_linux.c:171-201.
- `pwrseq._run_table` (`pwrseq.py`) — HALMAC `pwr_sub_seq_parser`: WRITE = read-modify-write,
  POLLING = read-until-masked-match, DELAY/READ = no-op, the USB intf filter drops SDIO/PCI rows.

## Caveats

- The 8821c power tables differ from 8822b's (no `0xFF0A/0xFF0B/0x0012` LDO rows, different PCI
  block, no cut-C `0x10A8` rows, ACT ends at `0x007C`). This is why the port is self-contained,
  not a reuse of `rtl8822bu_dkms`.
- Every 8821c card_en/dis row is `CUT_ALL`, so the chip cut does not filter the power sequence;
  the real cut (from `SYS_CFG1` 0xF0) only gates the init tables that follow.

## Known issues

- **M1 frontier = the chip-id / pre-init read block, which runs BEFORE power-on.** The gate
  diverges at op #0: our `power_on` first touches `0x004A`, but the wire opens (f534+) with
  reads of `SYS_CFG2` (0xFC), `SYS_CFG1` (0xF0 = chip ver/cut), `SYS_STATUS1` (0xF4), `0x0068`,
  `0x000A/0x0035/0x0037`, then an **indirect read loop via `0x0030`** (`write 0x316000NN` →
  `read 0xb16000XX`, NN incrementing). Port that prologue (the vendor `read_chip_version` /
  pre-init path) as milestone 1; the power sequence (already ported) then verifies as M2.
- `transport` bulk-OUT EP defaulted to `0x04` (not on the M1 path) — confirm against the
  coverage audit (`bulk-OUT ep 0x05`) at the FW/TX milestone.

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
