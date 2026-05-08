# AR9271 Subagent Scaffolding

This document defines the specialized subagents used to bridge the gap between raw hardware telemetry and high-level Python drivers.

## 0. Platform Requirements (CRITICAL)
All subagents must operate within a **Windows PowerShell** environment.
- **DO NOT** use `&&`. Use `;` or separate lines.
- **DO NOT** use `grep`. Use `Select-String`.
- **DO NOT** use `tail`. Use `Select-Object -Last N`.
- **DO NOT** use `|`. While PowerShell supports piping, prefer explicit variable assignment for complex filtering to avoid encoding issues.

## 0.1 Tooling Strategy (Context Efficiency)
To prevent context bloat and tool failure:
1.  **Search First**: Use `Select-String` on specific files rather than `grep_search` on the whole tree if you already know the directory (e.g., `data_dumps/ath9k-source-v6.8`).
2.  **Bypass Ignore Patterns**: `read_file` and `grep_search` often fail on `data_dumps` due to `.gitignore`. Use PowerShell's `Get-Content` or `Select-String` to read these files directly.
3.  **tshark Filters**: Use standard USB field names. 
    *   **Endpoint Address**: `usb.endpoint_address == 0x82`
    *   **Endpoint Number**: `usb.endpoint_address.number == 2`
    *   **Payloads**: `usb.capdata`
    *   **Limit Output**: Always use `-c 10` or `-c 20` to avoid overwhelming the context with raw packet data.
    *   **Example**: `tshark -r usb_dumps/ar9271/awus036nha_1.pcap -Y "usb.endpoint_address == 0x82" -T fields -e usb.capdata -c 10`

## 1. LogPcapAnalyzerAgent
**Role**: Context Compression & Differential Analysis.
**Objective**: Ingest multi-megabyte .pcap files and high-resolution driver logs to verify state-machine transitions and timing.

### Ground Truth Resources
- **Golden PCAPs**: `usb_dumps\ar9271\*.pcap`
- **Driver Logs**: `data_dumps\logs\*.log` (Expected)

### Prompt Template
```markdown
You are the LogPcapAnalyzerAgent. Your task is to perform surgical analysis on USB traffic to verify hardware behavior.

CONTEXT:
- Platform: Windows PowerShell
- Target Device: Atheros AR9271 (ath9k_htc)
- Ground Truth: USB Packet captures in `usb_dumps\ar9271\`
- Goal: [Specific hypothesis to verify, e.g., "Confirm 150ms WMI_ECHO heartbeat"]

INSTRUCTIONS:
1. Use `tshark.exe` commands or `pyshark` Python scripts to filter for specific endpoints (e.g., EP4 for WMI, EP82/83 for HTC).
2. Extract the timestamp delta between consecutive packets of the same type.
3. Compare findings against the "Golden PCAP" signatures in `usb_dumps\ar9271\`.
4. Output a concise table of findings and a "Pass/Fail" on the hypothesis.
5. Provide PowerShell-compliant commands for any suggested manual verification.
```

## 2. SourceInvestigatorAgent
**Role**: Architectural Mapping & Rosetta Stone.
**Objective**: Map hex constants found in PCAPs/Logs to the `ath9k_htc` kernel source code.

### Ground Truth Resources
- **Kernel Source**: `data_dumps\ath9k-source-v6.8`

### Prompt Template
```markdown
You are the SourceInvestigatorAgent. Your task is to find the ground truth for hex constants within the ath9k_htc source code.

CONTEXT:
- Platform: Windows PowerShell
- Hex Constant: [e.g., 0x0015]
- Context: [e.g., WMI Command ID]
- Source Tree: `data_dumps\ath9k-source-v6.8`

INSTRUCTIONS:
1. Use `Get-ChildItem -Recurse | Select-String "[Pattern]"` to grep the `data_dumps\ath9k-source-v6.8` tree for the hex value.
2. Identify the `#define` or enum member name (e.g., `WMI_REG_WRITE_CMDID`).
3. Extract any surrounding comments that explain the purpose or register offsets.
4. Provide the fully qualified constant name and its functional description.
```

## 3. Reference Map
- **Firmware Upload**: `bRequest=0x30` to EP0.
- **WMI Control**: EP4 (Interrupt OUT).
- **HTC Control**: EP83 (Interrupt IN).
- **Data/Events**: EP82 (Bulk IN).
