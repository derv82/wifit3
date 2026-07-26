# RTL8922AU firmware blob

`assets/rtw8922a_fw-4.bin` is the WLAN CPU firmware for the RTL8922A, uploaded during cold-boot
bring-up (`firmware.download`). It is a proprietary Realtek blob, not GPL, redistributed with the
driver.

- **File**: `rtw8922a_fw-4.bin` (Realtek multi-firmware `fw_format` 4 container).
- **Size**: 1849226 bytes.
- **SHA-256**: `d11927f593c82879bd0437435475d7915a60932374747984b3dc906a23009dea`.
- **Provenance**: taken from the capture bundle's `driver-source/firmware/` (the exact blob the
  recorded rtw89 driver uploaded), so the port byte-matches the capture. Upstream it ships in
  linux-firmware under `rtw89/`; see linux-firmware `WHENCE` for the Realtek redistribution terms.
- **Selection**: `firmware.load_fw_suit` parses the multi-firmware header and picks the `NORMAL`
  sub-firmware whose chip cut is the closest at or below `hal.cv` (this card: cut 1 at cv 2).

Do not swap this for a different `fw_format` or version: the cold-boot pcap was recorded against
this exact file, and `verify_pcap` byte-matches the header + section bulk-OUT transfers against it.
