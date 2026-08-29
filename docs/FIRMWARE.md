# Firmware provenance

Wifit3 is licensed **GPL-2.0-only**, but the 24 firmware blobs it ships under
`src/wifit3/chips/<chip>/assets/` (`*.bin`, plus the ath9k `*.fw`) are **not** GPL. Each is a vendor binary that the
silicon needs loaded at bring-up, redistributed here *verbatim* under its own
manufacturer's license, exactly as Linux's
[`linux-firmware`](https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git)
repository ships them, and documented the same way that repo's `WHENCE` manifest does.
None of these blobs are a derivative of the GPLv2 driver code we ported; they pass through
untouched.

Every blob was **byte-verified against its upstream** before shipping: either against the
canonical `linux-firmware` copy (SHA-256 / byte-diff), or against the vendor DKMS source's
embedded C array, and in most cases re-derived from the chip's own cold-boot pcap and
diffed against that copy. The per-chip ground-truth docs (`chips/<chip>/<CHIP>.md`) carry
the verification detail, frame ranges, and hashes; this file is the licensing summary.

## The blobs

| Blob | Chip(s) | Upstream source | Redistribution license | Byte-verified? |
|---|---|---|---|---|
| `rtw8821a_fw.bin` | RTL8821AU (mainline) | `linux-firmware` `rtw88/rtw8821a_fw.bin` | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ pcap body == wire == linux-firmware |
| `rtw8822b_fw.bin` | RTL8822BU (mainline) | `linux-firmware` `rtw88/rtw8822b_fw.bin` | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ vs `rtw8822b_fw.bin[FW_HDR_SIZE:]` |
| `rtw8812a_fw.bin` | RTL8812AU (mainline) | `linux-firmware` `rtw88/rtw8812a_fw.bin` | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ SHA-256 + pcap body diff |
| `rtw8814a_fw-linux_firmware.bin` | RTL8814AU (rtw88 mainline) | `linux-firmware` `rtw88/rtw8814a_fw.bin` | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ pcap-reassembled == `rtw8814a_fw.bin[64:]` |
| `rtl8188eufw.bin` | RTL8188EUS (mainline) | `linux-firmware` `rtlwifi/rtl8188eufw.bin` | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ SHA-256 match (pcap-extracted) |
| `rtl8812au_fw.bin` | RTL8812AU (vendor DKMS) | morrownr `8812au` DKMS `hal/rtl8812a` (`array_mp_8812a_fw_nic`) | Realtek redistributable (same Realtek FW terms) | ✅ golden-hash vs vendor C array (27030 B) |
| `rtl8814au_fw.bin` | RTL8814AU (vendor DKMS) | morrownr `8814au` DKMS `hal8814a_fw.c` (`array_mp_8814a_fw_nic`) | Realtek redistributable (same Realtek FW terms) | ✅ vendor C array == pcap bulk payload (68320 B) |
| `rtl8821au_fw.bin` | RTL8821AU (vendor DKMS) | [Lucid-Duck PR #194](https://github.com/morrownr/8821au-20210708/pull/194) `morrownr/8821au-20210708` DKMS `hal/rtl8821a` (vendor FW array) | Realtek redistributable (same Realtek FW terms) | ✅ FW page-write byte-exact in `verify_pcap` (30880 B) |
| `rtl8822bu_fw.bin` | RTL8822BU (vendor DKMS) | morrownr `88x2bu` DKMS (`array_mp_8822b_fw_nic` v30.20) | Realtek redistributable (same Realtek FW terms) | ✅ download byte-exact in `verify_pcap` (161240 B) |
| `rtl8188eufw.bin` (dkms) | RTL8188EUS (vendor DKMS) | aircrack-ng/kimocoder `8188eus` DKMS (`array_mp_8188e_t_fw_nic`) | Realtek redistributable (`LICENCE.rtlwifi_firmware.txt`) | ✅ SHA-256 == linux-firmware copy |
| `WIFI_MT7961_patch_mcu_1_2_hdr.bin` | MT7921AU | `linux-firmware` `mediatek/WIFI_MT7961_patch_mcu_1_2_hdr.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ SHA-256 vs linux-firmware |
| `WIFI_RAM_CODE_MT7961_1.bin` | MT7921AU | `linux-firmware` `mediatek/WIFI_RAM_CODE_MT7961_1.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ SHA-256 vs linux-firmware |
| `WIFI_MT7925_PATCH_MCU_1_1_hdr.bin` | MT7925AU | `linux-firmware` `mediatek/mt7925/WIFI_MT7925_PATCH_MCU_1_1_hdr.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ FW download byte-exact in `verify_pcap` (197792 B, sha256 `8b68c73d…`) |
| `WIFI_RAM_CODE_MT7925_1_1.bin` | MT7925AU | `linux-firmware` `mediatek/mt7925/WIFI_RAM_CODE_MT7925_1_1.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ FW download byte-exact in `verify_pcap` (1246968 B, sha256 `f156ca10…`) |
| `mt7662_ilm.bin` | MT7612U (MT76x2U) | `linux-firmware` `mediatek/mt7662.bin` (ILM region) | Ralink/MediaTek redistributable (`LICENCE.ralink_a_mediatek_company_firmware`) | ✅ == `mt7662.bin[32:32+ilm_len]` |
| `mt7662_dlm.bin` | MT7612U (MT76x2U) | `linux-firmware` `mediatek/mt7662.bin` (DLM region) | Ralink/MediaTek redistributable (`LICENCE.ralink_a_mediatek_company_firmware`) | ✅ == `mt7662.bin` DLM region |
| `mt7662_rom_patch_body.bin` | MT7612U (MT76x2U) | `linux-firmware` `mediatek/mt7662_rom_patch.bin` (30-B header stripped) | Ralink/MediaTek redistributable (`LICENCE.ralink_a_mediatek_company_firmware`) | ✅ == `mt7662_rom_patch.bin[30:]` |
| `mt7610e_linux-firmware.bin` | MT7610U (MT76x0U) | `linux-firmware` `mediatek/mt7610e.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ pcap body == `mt7610e.bin[32:]` |
| `mt7610u_linux-firmware.bin` | MT7610U (MT76x0U) | `linux-firmware` `mediatek/mt7610u.bin` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ shipped verbatim (kernel fallback variant) |
| `mt7610u_pcap_body.bin` | MT7610U (MT76x0U) | extracted from cold-boot pcap; == `mt7610e.bin[32:]` | MediaTek redistributable (`LICENCE.mediatek`) | ✅ byte-for-byte vs `mt7610e.bin[32:]` |
| `rt5572.bin` | RT5572 / RT3572 (RT2800USB) | `linux-firmware` `rt2870.bin` (USB half, offset 4096) | Ralink redistributable (`LICENCE.ralink-firmware.txt`) | ✅ md5 `8d98ca9f…`, second 4 KB of `rt2870.bin` |
| `rt3070_fw.bin` | RT3070 | `linux-firmware` `rt2870.bin` (first 4 KB, offset 0) | Ralink redistributable (`LICENCE.ralink-firmware.txt`) | ✅ md5 `d94f0280…`, wire multiwrite == `rt2870.bin[:4096]` |
| `rt5372_fw.bin` | RT5372 | `linux-firmware` `rt2870.bin` (USB half, offset 4096) | Ralink redistributable (`LICENCE.ralink-firmware.txt`) | ✅ md5 `8d98ca9f…`, `rt2870.bin[4096:]` |
| `rt5370_fw.bin` | RT5370 | `linux-firmware` `rt2870.bin` (USB half, offset 4096) | Ralink redistributable (`LICENCE.ralink-firmware.txt`) | ✅ md5 `8d98ca9f…`, `rt2870.bin[4096:]` (byte-identical to `rt5372_fw.bin`) |
| `htc_9271-1.4.0.fw` | AR9271 (ath9k_htc, v2) | `linux-firmware` `ath9k_htc/htc_9271-1.4.0.fw` | Atheros redistributable (`LICENCE.atheros_firmware`) | ✅ md5 `4ed467d4…`, wire FW-download (13 chunks) == blob on all 3 pcaps |

Notes:

- **One blob shared, two slices.** The Ralink `rt3070_fw.bin`, `rt5370_fw.bin`, `rt5372_fw.bin`,
  and `rt5572.bin` are all carved from the single `linux-firmware` `rt2870.bin`: RT3070 uses the
  first 4 KB (PCI half), RT5370/RT5372/RT5572 use the second 4 KB (USB half). In `WHENCE`,
  `rt3070.bin` is a symlink to `rt2870.bin`, and `rt3071.bin` is documented as "a copy of
  bytes 4096-8191 of rt2870.bin."
- **RTL8814AU.** Both the mainline (`rtw88_8814au`) and the vendor-DKMS (`rtl8814au_dkms`)
  ports carry an 8814a blob; they are the same 68320-byte Realtek firmware. `linux-firmware`
  now ships it as `rtw88/rtw8814a_fw.bin`; the DKMS copy was verified against the vendor C
  array (`array_mp_8814a_fw_nic`).
- **Vendor-DKMS Realtek blobs** are embedded as C `data` arrays inside the GPLv2 DKMS
  driver source rather than shipped as standalone files. The *driver code* is GPLv2; the
  embedded firmware payload is Realtek's redistributable binary and is governed by the same
  Realtek firmware terms as the mainline copies (`LICENCE.rtlwifi_firmware.txt`). We extract
  the array to a `.bin` and ship it unmodified.
- **MT7612U firmware is `mt7662.bin`, not `mt7662u.bin`.** The mainline `mt76x2u` USB driver
  requests `MT7662_FIRMWARE` / `MT7662_ROM_PATCH` = `mt7662.bin` / `mt7662_rom_patch.bin`
  (`mt76x2/mt76x2.h`, `usb_mcu.c`), the same files the PCIe `mt76x2e` driver uses. Our three
  blobs are byte-identical to them (ILM+DLM == `mt7662.bin[32:]`; rom-patch body ==
  `mt7662_rom_patch.bin[30:]`, the 30-byte `mt76x02_patch_header` stripped). The similarly
  named `mt7662u.bin` (a different, larger build that `WHENCE` files under `mt76x2u` /
  `LICENCE.mediatek`) is *not* what mainline loads or what wifit3 ships, called out here so
  the provenance and the governing license stay correct.

## Per-vendor license summary

The blobs group under four vendor licenses, mirroring `linux-firmware`'s `WHENCE`. The
verbatim `linux-firmware` `LICENCE.*` file for each blob now ships next to it in that chip's
`assets/` directory, so the license travels with the binary in the repo, the built wheel, and
the PyInstaller bundle. The governing-license wording quoted below is from that same file.

### Realtek — `LICENCE.rtlwifi_firmware.txt`

Covers the `rtw88/rtw88xx` and `rtlwifi/rtl8188eufw` blobs, and (by the same Realtek terms)
the vendor-DKMS Realtek blobs. `WHENCE` marks every one:

> Licence: Redistributable. See LICENCE.rtlwifi_firmware.txt for details.

The license itself (Copyright (c) 2010, Realtek Semiconductor Corporation) permits
redistribution and use **in binary form, without modification**, provided redistributions
reproduce the copyright notice and disclaimer; it explicitly forbids reverse engineering,
decompilation, or disassembly, and grants a limited patent license for use alone or in
combination with an OSI-approved open-source-licensed operating system.

### MediaTek — `LICENCE.mediatek`

Covers the MT7921AU (`WIFI_MT7961_*`, `WIFI_RAM_CODE_MT7961_*`), MT7925AU
(`WIFI_MT7925_PATCH_MCU_1_1_hdr`, `WIFI_RAM_CODE_MT7925_1_1`, filed under `mediatek/mt7925/`),
and MT7610U (`mt7610e`/`mt7610u`) blobs. (The MT7612U `mt7662*` blobs are **not** here:
`WHENCE` files them under the Ralink/MediaTek license below.) `WHENCE` marks the MediaTek blobs:

> Licence: Redistributable. See LICENCE.mediatek for details.

The license reads, in full:

> MediaTek Inc. grants permission to use and redistribute aforementioned firmware files for
> the use with devices containing MediaTek chipsets, but not as part of the Linux kernel or
> in any other form which would require these files themselves to be covered by the terms of
> the GNU General Public License or the GNU Lesser General Public License.

(Distributed WITHOUT ANY WARRANTY.) This is the explicit upstream basis for keeping the
blobs *out* of the GPL boundary: they are bundled data, not part of the GPL-2.0-only
codebase.

### Ralink — `LICENCE.ralink-firmware.txt`

The RT2800USB-family blobs (`rt2870.bin` slices: `rt3070_fw.bin`, `rt5370_fw.bin`,
`rt5372_fw.bin`, `rt5572.bin`) fall under the original Ralink firmware license. `WHENCE` marks
`rt2870.bin`:

> Licence: Redistributable. See LICENCE.ralink-firmware.txt for details

The license (Copyright (c) 2007, Ralink Technology Corporation) permits redistribution and
use **in binary form, without modification** provided redistributions reproduce the
copyright notice and disclaimer; no reverse engineering, decompilation, or disassembly; with
the same limited patent license as the Realtek terms above.

### Ralink / MediaTek — `LICENCE.ralink_a_mediatek_company_firmware`

Covers the MT7612U (MT76x2U) blobs: `mt7662_ilm.bin` + `mt7662_dlm.bin` (carved from
`mt7662.bin`) and `mt7662_rom_patch_body.bin` (from `mt7662_rom_patch.bin`). The card is USB,
but the mainline `mt76x2u` driver loads the same `mt7662.bin` / `mt7662_rom_patch.bin` the
PCIe `mt76x2e` driver does, and `WHENCE` files those under driver `mt76x2e`:

> Licence: Redistributable. See LICENCE.ralink_a_mediatek_company_firmware for details

The license (Copyright (c) 2013, Ralink, A MediaTek Company) carries terms identical to the
2007 Ralink license above: binary redistribution provided the copyright notice and disclaimer
are reproduced, no reverse engineering, and the same limited patent license.

---

*Every new firmware blob added under `chips/<chip>/assets/` must be recorded in this file:
its provenance and redistribution terms from the `linux-firmware` `WHENCE` manifest, and a
byte-verification against `linux-firmware` (or the vendor source it was extracted from). See
`docs/porting/METHODOLOGY.md` → "Housekeeping — every new port" → Licensing.*
