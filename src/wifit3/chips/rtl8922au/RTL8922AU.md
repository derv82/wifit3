# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 4265/163814 driver ops reproduced and committed (capture-1; 4286/4300 on
capture-2/3, poll-count variance only). `verify_pcap` now walks both VENQT control ops and
bulk-OUT ops; all three captures stop at the same frontier. What is ported:
- USB register-access transport (`rtw89_usb_vendorreq` + read/write ops + `read_cmac`), the
  read-modify-write helpers (`write8/16/32_set/clr`, `write16/32_mask`), and `bulk_out`.
- USB mode-switch (`rtw89_usb_switch_mode`, speed-branched) + `read_chip_ver`.
- MAC power-on: `rtw89_mac_pwr_on` -> `power_switch(on=True)` in full (boot-mode handoff,
  `reset_pwr_state_be` all three MAC-state arms, `rtw8922a_pwr_on_func`, then the first-probe
  efuse reads and the coex scoreboard notify).
- `rtw89_mac_partial_init(include_bb=False)`: HCI/DMAC pre-en, `dle_init(QTA_DLFW)`, `hfc_init`,
  `fwdl_preconfig`, then `rtw89_fw_download`.
- **Firmware download** (`firmware.py`): multi-firmware container parse, v1 header parse with the
  formatted-MSSC security sections, the H2C/fwdl packet build (24-byte TX descriptor + 8-byte
  fwcmd header + tweaked firmware header), and the section transfers over bulk-OUT ep 0x07. The
  header + all 212 section packets byte-match. Blob in `assets/rtw8922a_fw-4.bin` (see FIRMWARE.md).
- `parse_efuse_map` (physical 0x1300 dump + USB MAC read from 0x4078) + `parse_phycap_map`
  (0x38 dump at 0x1700). The RF/board logical extraction is deferred (software, no wire ops).

Frontier: op #4274, `read 0x68` (a third `cnv_efuse_state` opening another efuse-style dump; this
is inside `rtw89_mac_setup_phycap` / `rtw89_mac_read_phycap`, the H2C+phycap query to the running
firmware). MAC init, RF/BB init, channel tune, and TX are not yet ported.

### Gotchas found while porting (not obvious from a single read)

- The cold-boot capture takes the **boot-mode branch** of `power_switch_boot_mode`, and
  `reset_pwr_state_be` finds the MAC already **`MAC_ON`**, so it runs the MAC-on arm.
- `dle_init(DLFW)` calls `get_dle_mem_cfg(ext_mode=SCC)` last, which sets `dle_info.qta_mode = SCC`.
  So `hfc_reset_param` reads back **SCC** and the H2C page precedence is `hfc_prec_cfg_c5` (32),
  not DLFW's c2. State-order matters.
- `rtw89_mac_partial_init` ends with `rtw89_fw_download` inside it; firmware is downloaded during
  `chip_efuse_info_setup`, before `parse_efuse_map`. `wait_firmware_completion` / `fw_recognize`
  are file-side (no wire ops).
- The firmware header packet's only tweak vs the raw file is `w6` SEC_NUM 4->3: two security
  sections exist and the second (last) is marked `ignore`, compacted out, and the header trimmed
  16 bytes to 96. The `.bin` is a multi-firmware container; the NORMAL sub-firmware for cut 1 (at
  `hal.cv`=2) starts at shift 64.
- The two `fw_check_rdy` calls differ: WCPU-FWDL-DONE stops when `B_BE_WLANCPU_FWDL_EN` clears;
  FREERTOS-DONE stops when the status field (bits 26-29) reads raw 3. Do not merge them (the
  merged OR condition ends the FreeRTOS poll early on some captures).
- pcap_slicer maps frames 1-178 to enumeration (the 9 waived ops); the whole register bring-up
  runs under the first `airmon-ng start` phase. Bulk-OUT ep 0x07 = `out_pipe[bulkout_id[H2C]=2]`.

## Source

`/usr/src/rtw89-7.2` (morrownr rtw89 v7.2, installed via DKMS, persists across sessions). Port
from THIS, not from the mt7921au sibling in this tree (methodology forbids porting from a
sibling driver). Key files:
- `usb.c` the USB probe (`rtw89_usb_probe`), register access (`rtw89_usb_vendorreq`), mode
  switch (`rtw89_usb_switch_mode_be`).
- `core.c` `rtw89_read_chip_ver`, `rtw89_core_init` (the post-switch bring-up).
- `mac.c` `rtw89_mac_read_xtal_si_ax`, the power-on sequence.
- `reg.h` / `mac.h` register addresses and bitfields (paste verbatim, cite `file:line`).

`0b05:1d84` was added to `rtw8922au.c`'s id table so the kernel driver binds; the card runs as
a `wlan` interface under `rtw89_8922au_git` for hardware testing and re-captures later.

## Capture

`usb_dumps_new2/captures_rtw89_8922au_git/` (capture-1/2/3, cold boot). Taken on a USB-2 path:
`rtw89_usb_switch_mode` early-returns on SuperSpeed, so `switch_mode_be` reads `R_BE_PAD_CTRL2`
and the pcap opens with that read. Verify against all three per the methodology's step 6.

## Verify

    uv run python scripts/verify_pcap.py rtl8922au [capture]

One forward cursor over the device's VENQT control ops, driving the real `connect()`. Ops the
driver never emits (USB enumeration) are waived by name and logged, never dropped. It cannot
report PASS until every register op reproduces; on a mismatch it prints the frontier with a
10-before/after trace. `ReplayDev.speed = 3` (USB-2) so the mode-switch runs; a USB-C
(SuperSpeed) capture would set speed 4 and skip the `PAD_CTRL2` read. Only VENQT control ops
are walked so far. Extend `build_ops` for bulk-OUT (firmware chunks, TX) when the port reaches
them.

## Register access

A register op is a vendor control transfer on endpoint 0, `bRequest = 0x05` (`RTW89_USB_VENQT`),
`bmRequestType = 0xC0` read / `0x40` write. The address splits across the setup packet as
`wValue = addr & 0xFFFF`, `wIndex = (addr >> 16) & 0xFF`. [SRC] usb.c:31-32.

CMAC-window reads (`0xC000..0xFFFF`) can return `0xDEADBEEF` until the CMAC clock is enabled;
`read_cmac` re-enables it and re-reads. [SRC] usb.c:83-108. Indirect crystal-SI registers go
through `read_xtal_si` (write a command to `XTAL_SI_CTRL`, poll, read the data field).

## Next (from op #4274)

`rtw89_mac_setup_phycap` (mac.c:3222-3335): `setup_phycap_part0`/`part1` call
`rtw89_mac_read_phycap`, an H2C query to the now-running firmware whose C2H reply is read back;
the frontier's `cnv_efuse_state`/dump-shaped ops belong to that path. This is the first H2C
*command* (not fwdl): port `rtw89_h2c_tx` for a normal H2C (via `rtw89_h2c_pkt_set_hdr`, not the
fwdl header) and the C2H read. Then `rtw89_core_setup_phycap` (sw), `hci_mac_pre_deinit` (USB
no-op), and chip_info_setup finishes with `rtw89_fw_recognize_elements` (BB/RF firmware element
tables from the file) + `rtw89_mac_pwr_off`. After that the real `rtw89_core_start` path runs
`rtw89_mac_init` (DMAC/CMAC full init), BB/RF init + calibration, channel tune, and monitor RX.
Keep verifying each against all three captures; the RF/board logical-efuse extraction deferred in
`parse_efuse_map` will be needed once RF init reads `efuse->rfe_type` etc.

## Style

Port from source, cite `file:line`. No milestone labels or status text in code (those live in
the commit message and this doc). Docstrings two lines or fewer, name things instead of
describing them, no jargon. No em-dashes and none of the banned words anywhere; see
`~/.claude/CLAUDE.md` and `docs/porting/CODE-STYLE.md`.

## Log

- 2026-07-26 M1: register-access transport + `Driver` subclass, full `rtw_8922au_id_table`.
- 2026-07-26 M2: USB mode-switch (USB-C/USB-2 speed branch) + `read_chip_ver`. 9 ops reproduced.
- 2026-07-26 M3: MAC power-on (boot-mode, `reset_pwr_state_be`, `pwr_on_func`). 121 ops.
- 2026-07-26 M4: power-on tail (efuse ecv/secure reads, coex scoreboard). 178 ops.
- 2026-07-26 M5a: DMAC pre-init (`hci_func_en`, `dmac_func_pre_en`). 188 ops.
- 2026-07-26 M5b: `dle_init` (QTA_DLFW quota subsystem). 218 ops.
- 2026-07-26 M5c: `hfc_init` + `fwdl_preconfig` finish `mac_partial_init`. 227 ops.
- 2026-07-26 M6a: WLAN-CPU disable + firmware-download enable (`disable_cpu`, `fwdl_enable_wcpu`).
  271 ops. Frontier at `fw_download_suit` (bulk-OUT firmware transfer next).
- 2026-07-26 M6-pre: `verify_pcap` bulk-OUT support + `fw_download_suit` pre-transfer control ops
  (secure-boot malloc, H2C path-ready). 274 ops.
- 2026-07-26 M6b: firmware download (mfw parse, v1 header + security sections, TX-desc/H2C-fwdl
  packet build, section bulk-OUT, ready polls). 521 ops. Blob `assets/rtw8922a_fw-4.bin`.
- 2026-07-26 M7: `parse_efuse_map` (0x1300 dump + USB MAC read) + `parse_phycap_map` (0x38 dump).
  4265 ops. Frontier at `setup_phycap` (`rtw89_mac_read_phycap` H2C next).
