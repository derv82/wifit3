# Wifit3 Subagent Scaffolding

This document defines the specialized subagents and technical workflows used to bridge the gap between raw hardware telemetry and high-level Python drivers.

## 0. Platform Requirements (CRITICAL)
All subagents MUST operate within a **Windows PowerShell** environment.
- **DO NOT** use `&&`. Use `;` or separate lines.
- **DO NOT** use `grep`. Use `rg` (ripgrep) which is available in the environment.
- **DO NOT** use `tail`. Use `Select-Object -Last N`.
- **DO NOT** use `-c` with `tshark` 

## 1. Tooling Strategy (Context Efficiency)
To prevent context bloat and tool failure, always use the deterministic scripts in `./scratch/`:

1. **Source Intelligence**: Use `python scratch/source_intel.py <source_dir> <hex_or_token>` for mapping telemetry to code.
   - It automatically finds registers, bitfields, and parent/child relationships.
   - It summarizes results if more than 10 matches are found to prevent context flooding.
2. **PCAP Slicing**: Use `python scratch/pcap_slicer.py <log_file> <pcap_file>` BEFORE analyzing a pcap.
   - This maps commands in `main.log` to exact frame boundaries in the `.pcap` based on absolute Epoch time.
3. **Traffic Analysis**: Use `tshark.exe` for specific frame extraction.
   - **Endpoint Address**: `usb.endpoint_address == 0x82`
   - **Control Transfers**: `usb.setup.bRequest`, `usb.setup.wValue`, `usb.setup.wIndex`.
   - **Payloads**: `usb.capdata`
   - **Limit Output**: Use `| Select-Object -First N` in PowerShell. NEVER use tshark's `-c` unless you are specifically trying to limit *the total number of packets read from the file*. It **stops the entire process** after N packets, it does **not** filter results.

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
3. **Intel**: Use `source_intel.py` on the hex values (addresses/masks) found in those frames to identify their names and purposes in the kernel source.
4. **Synthesis**: Update the Python driver's `constants.py` and `sequences/` logic based on findings.

### Prompt Template
```markdown
You are the HardwareReverseEngineer. Your task is to verify hardware behavior by aligning logs with USB traffic.

CONTEXT:
- Target Chipset: [e.g., RT5572]
- Goal: [Specific hypothesis, e.g., "Analyze register pokes during channel hop to 1"]

INSTRUCTIONS:
1. REQUIRED FIRST STEP: Run `python scratch/pcap_slicer.py <log_file> <pcap_file>` to determine the EXACT frame number boundaries for the target command.
2. Extract unique packets using `tshark.exe` within the slice bounds.
3. Map every unique register address or bitmask found in the traffic to the kernel source using `python scratch/source_intel.py data_dumps/<chip_source> <hex_value>`.
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
