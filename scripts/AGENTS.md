# Wifit3 Subagent Scaffolding

This document defines the specialized subagents and technical workflows used to bridge the gap between raw hardware telemetry and high-level Python drivers.

## 0. Platform Requirements (CRITICAL)
All subagents MUST operate within a **Windows PowerShell** environment.
- **DO NOT** use `&&`. Use `;` or separate lines.
- **DO NOT** use `grep`. Use `rg` (ripgrep) which is available in the environment.
- **DO NOT** use `tail`. Use `Select-Object -Last N`.
- **DO NOT** use `-c` with `tshark` 

## 1. Tooling Strategy (Context Efficiency)
The deterministic scripts in `./scripts/` are the source of truth for hardware-level lookups:

1. **PCAP Slicing**: Use `python scripts/pcap_slicer.py <log_file> <pcap_file>` BEFORE analyzing a pcap.
   - This maps commands in `main.log` to exact frame boundaries in the `.pcap` based on absolute Epoch time.
2. **Source Intelligence**: Use `Grep` / `Read` directly against `data_dumps/<chipset>-source/` for register/macro lookups.
   - Example: `Grep "#define\s+REG_FOO" data_dumps/rtw88-source-v6.18/ --glob "*.h" -A 3`
   - Match the exact `BIT(n)` macros — never infer bit positions from names (see `feedback_grep_bits_dont_guess` memory).
3. **Traffic Analysis**: Use `tshark.exe` for specific frame extraction.
   - **Endpoint Address**: `usb.endpoint_address == 0x82`
   - **Control Transfers**: `usb.setup.bRequest`, `usb.setup.wValue`, `usb.setup.wIndex`.
   - **Payloads**: `usb.capdata`
   - **Limit Output**: Use `| Select-Object -First N` in PowerShell. NEVER use tshark's `-c` unless you are specifically trying to limit *the total number of packets read from the file*. It **stops the entire process** after N packets, it does **not** filter results.
4. **Control-transfer diff** (rt2800usb family): `python scripts/rt2800usb/rt2800_ctrl_diff.py` decodes vendor control transfers and diffs the kernel's bring-up sequence against ours.
5. **Frame payload peek**: `python scripts/peek_frame.py <pcap> <frame_no>...` dumps the raw payload of specific frame numbers.

## 2. HardwareReverseEngineer
**Role**: Protocol Analyst & State Machine Mapper.
**Objective**: Ingest multi-megabyte .pcap files and high-resolution driver logs to verify state-machine transitions and timing.

### Ground Truth Resources
- **PCAPs**: `usb_dumps/**/*.pcap`
- **Driver Logs**: `usb_dumps/**/logs/*.log` (e.g., `main.log`, `airmon-ng.log`, `iw.log`)
- **Kernel Source**: `data_dumps/[chipset]-source/`

### Workflow: Log + PCAP = Truth
1. **Slicing**: Run `pcap_slicer.py` to find the `start_frame` and `end_frame` for the target command (e.g., "Set Channel 6").
2. **Extraction**: Use `tshark` to extract the unique frames within that range.
3. **Intel**: Map every unique register address or bitmask to the kernel source via `Grep` / `Read` against `data_dumps/<chip_source>/`.
4. **Synthesis**: Update the Python driver's `constants.py` and `sequences/` logic based on findings.

### Prompt Template
```markdown
You are the HardwareReverseEngineer. Your task is to verify hardware behavior by aligning logs with USB traffic.

CONTEXT:
- Target Chipset: [e.g., RT5572]
- Goal: [Specific hypothesis, e.g., "Analyze register pokes during channel hop to 1"]

INSTRUCTIONS:
1. REQUIRED FIRST STEP: Run `python scripts/pcap_slicer.py <log_file> <pcap_file>` to determine the EXACT frame number boundaries for the target command.
2. Extract unique packets using `tshark.exe` within the slice bounds.
3. Map every unique register address or bitmask found in the traffic to a symbol in `data_dumps/<chip_source>/` using `Grep`.
4. Output a concise table of Findings: [Frame #] | [Source Symbol] | [Value/Action] | [Result].
5. Provide the updated `constants.py` entries or sequence steps.
```

## 3. Reference Map (Common Chipset Patterns)
- **Ralink (rt2x00)**:
  - `bRequest=0x06`: Multi-Write (Registers)
  - `bRequest=0x07`: Multi-Read (Registers)
  - `bRequest=0x09`: EEPROM Read
  - `0x0400`: `PBF_SYS_CTRL` (Post-Boot)
  - `0x1000`: `MAC_CSR0` (ASIC Version)
- **Atheros (ath9k_htc)**:
  - `bRequest=0x30`: Firmware Upload
  - EP4 (Interrupt OUT): WMI Commands
  - EP82 (Bulk IN): HTC Events
- **Realtek (rtl8187)**:
  - `wValue`/`wIndex` are often literal register offsets.
