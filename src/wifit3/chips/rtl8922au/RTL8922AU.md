# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 271/162623 register ops reproduced and committed (capture-1; 272/273 on
capture-2/3, poll-count variance only). All three stop at the same frontier. What is ported:
- USB register-access transport (`rtw89_usb_vendorreq` + read/write ops + `read_cmac`), plus the
  read-modify-write helpers (`write8/16/32_set/clr`, `write16/32_mask`).
- USB mode-switch (`rtw89_usb_switch_mode`, speed-branched) + `read_chip_ver`.
- MAC power-on: `rtw89_mac_pwr_on` -> `power_switch(on=True)` in full (boot-mode handoff,
  `reset_pwr_state_be` all three MAC-state arms, `rtw8922a_pwr_on_func`, then the first-probe
  efuse reads `efuse_read_ecv`/`efuse_read_fw_secure` and the coex scoreboard notify).
- `rtw89_mac_partial_init(include_bb=False)` (from `rtw89_chip_efuse_info_setup`): HCI/DMAC
  pre-en, `dle_init(QTA_DLFW)`, `hfc_init`, `fwdl_preconfig`.
- Firmware-download open: `disable_cpu` + `fwdl_enable_wcpu` (`set_cpu_en` + `wcpu_on`).

Frontier: op #280, `write 0x184 = 0x20248000` (the 8922A secure-boot malloc write that opens
`rtw89_fw_download_suit`). The firmware bulk transfer, MAC init, RF/BB init, channel tune, and TX
are not yet ported.

### Gotchas found while porting (not obvious from a single read)

- The cold-boot capture takes the **boot-mode branch** of `power_switch_boot_mode` (the boot-ROM
  handoff bit is set), and `reset_pwr_state_be` finds the MAC already **`MAC_ON`**, not off, so it
  runs the MAC-on arm.
- `dle_init(DLFW)` calls `get_dle_mem_cfg(ext_mode=SCC)` last, which sets `dle_info.qta_mode = SCC`.
  So `hfc_reset_param` (in `hfc_init`) reads back **SCC**, and the H2C page precedence comes from
  the USB SCC config `hfc_prec_cfg_c5` (h2c_prec=32), not DLFW's c2. State-order matters here.
- `rtw89_mac_partial_init` ends with `rtw89_fw_download` (fw.c) inside it; the firmware is
  downloaded during `chip_efuse_info_setup`, before `parse_efuse_map`. `wait_firmware_completion`
  and `fw_recognize` are file-side (no wire ops).
- pcap_slicer maps frames 1-178 to `<hardware_plugin_and_initialization>` (pure USB enumeration,
  the 9 waived ops); the whole register bring-up runs under the first `airmon-ng start` phase.

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

## Next (from op #280)

`rtw89_fw_download_suit` (fw.c:1948): the 8922A secure-boot malloc write (op #280), then
`fwdl_check_path_ready` (poll `B_BE_DLFW_PATH_RDY`), `rtw89_fw_download_hdr`, and
`rtw89_fw_download_main`. The header and body go out over **bulk-OUT** (the FW sections), which
the VENQT-only `verify_pcap` does not yet walk: extend `build_ops` to include the bulk-OUT URBs
(usbmon xfer type 0x03) before porting this, per the methodology's step 3. The firmware blob is
`/usr/lib/firmware/rtw89/rtw8922a_fw.bin` (also in the bundle's `driver-source/firmware/`); parse
it with `rtw89_fw_hdr_parser`. After the transfer, `fw_check_rdy(FREERTOS_DONE)` polls
`R_BE_WCPU_FW_CTRL`. Then `parse_efuse_map`/`parse_phycap_map` (a 0x1300-byte logical-efuse dump
via the DDV path already ported in `dump_physical_efuse_map`), and on to `rtw89_mac_init`.

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
