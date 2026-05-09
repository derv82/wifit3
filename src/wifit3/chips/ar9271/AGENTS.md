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
1.  **Deterministic Tools**: ALWAYS use the Python scripts in `./scratch/` to interact with complex data.
    *   Use `python scratch/source_grep.py <dir/file> --token <token>` or `--hex` for surgical source code searches. Do NOT use raw grep/Select-String.
    *   Use `python scratch/pcap_slicer.py <log_file> <pcap_file>` BEFORE analyzing a pcap to map commands to exact frame boundaries based on absolute Epoch time.
2.  **Bypass Ignore Patterns**: `read_file` and `grep_search` often fail on `data_dumps` due to `.gitignore`. Use the `scratch/source_grep.py` script.
3.  **tshark Filters**: Use standard USB field names. 
    *   **Frame Ranges**: Always use `frame.number >= X and frame.number <= Y` based on `pcap_slicer.py` output.
    *   **Endpoint Address**: `usb.endpoint_address == 0x82`
    *   **Endpoint Number**: `usb.endpoint_address.number == 2`
    *   **Payloads**: `usb.capdata`
    *   **Limit Output**: Always use `-c 10` or `-c 20` to avoid overwhelming the context with raw packet data.
    *   **Example**: `tshark -r usb_dumps/ar9271/awus036nha_1.pcap -Y "frame.number >= 1543 and frame.number <= 6406 and usb.endpoint_address == 0x82" -T fields -e usb.capdata -c 10`

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
- Ground Truth: USB Packet captures and Logs in `usb_dumps\`
- Goal: [Specific hypothesis to verify, e.g., "Analyze register pokes during channel hop to 1"]

INSTRUCTIONS:
1. REQUIRED FIRST STEP: Run `python scratch/pcap_slicer.py <log_file> <pcap_file>` to determine the EXACT frame number boundaries for the target command. NEVER guess the offsets or rely on relative time.
2. Use `tshark.exe` commands to extract frames strictly within the `start_frame` and `end_frame` bounds identified in step 1. Filter for specific endpoints (e.g., EP4 for WMI, EP82/83 for HTC).
3. Extract the timestamp delta between consecutive packets of the same type.
4. Compare findings against the "Golden PCAP" signatures.
5. Output a concise table of findings and a "Pass/Fail" on the hypothesis.
6. Provide PowerShell-compliant commands for any suggested manual verification.
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
- Hex Constant/Token: [e.g., 0x0015 or WMI_REG_WRITE_CMDID]
- Source Tree: `data_dumps\ath9k-source-v6.8`

INSTRUCTIONS:
1. REQUIRED FIRST STEP: Use `python scratch/source_grep.py data_dumps/ath9k-source-v6.8 --token [Pattern]` to surgically locate the constant. DO NOT use raw PowerShell grep/Select-String.
2. Identify the `#define` or enum member name (e.g., `WMI_REG_WRITE_CMDID`).
3. Extract any surrounding comments that explain the purpose or register offsets.
4. Provide the fully qualified constant name and its functional description.
```

## 3. Reference Map
- **Firmware Upload**: `bRequest=0x30` to EP0.
- **WMI Control**: EP4 (Interrupt OUT).
- **HTC Control**: EP83 (Interrupt IN).
- **Data/Events**: EP82 (Bulk IN).
