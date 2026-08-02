"""Extract the AR9271 initvals tables from ar9002_initvals.h into a Python module.

A dev tool, run once (and re-run if the source tag changes). Parsing the C header rather than
hand-transcribing keeps the ~630 register/value rows byte-exact. Emits
src/wifit3/chips/ar9271_v2/initvals.py with MODES_9271 / COMMON_9271 / MODES_9271_ANI_reg.

    uv run python scripts/ar9271_v2/gen_initvals.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "driver_sources" / "ath9k-source-v6.18.12" / "ar9002_initvals.h"
OUT = REPO / "src" / "wifit3" / "chips" / "ar9271_v2" / "initvals.py"

TABLES = ["ar9271Modes_9271", "ar9271Common_9271", "ar9271Modes_9271_ANI_reg",
          "ar9271Modes_normal_power_tx_gain_9271", "ar9271Modes_high_power_tx_gain_9271"]
_PY_NAME = {"ar9271Modes_9271": "MODES_9271", "ar9271Common_9271": "COMMON_9271",
            "ar9271Modes_9271_ANI_reg": "MODES_9271_ANI_reg",
            "ar9271Modes_normal_power_tx_gain_9271": "MODES_NORMAL_POWER_TX_GAIN_9271",
            "ar9271Modes_high_power_tx_gain_9271": "MODES_HIGH_POWER_TX_GAIN_9271"}


def extract(text: str, name: str) -> list[list[int]]:
    m = re.search(rf"static const u32 {re.escape(name)}\[\]\[(\d+)\] = \{{(.*?)\}};",
                  text, re.DOTALL)
    if not m:
        raise SystemExit(f"table {name} not found")
    ncol = int(m.group(1))
    rows = []
    for row in re.finditer(r"\{([^}]*)\}", m.group(2)):
        vals = [int(v, 16) for v in re.findall(r"0x[0-9a-fA-F]+", row.group(1))]
        if len(vals) != ncol:
            raise SystemExit(f"{name}: row has {len(vals)} cols, expected {ncol}: {vals}")
        rows.append(vals)
    return rows


def main() -> None:
    text = SRC.read_text()
    blocks = []
    for name in TABLES:
        rows = extract(text, name)
        body = ",\n    ".join("[" + ", ".join(f"0x{v:08x}" for v in r) + "]" for r in rows)
        blocks.append(f"# {name}: {len(rows)} rows x {len(rows[0])} cols\n"
                      f"{_PY_NAME[name]} = [\n    {body},\n]")
    header = ('"""AR9271 initvals tables — generated from ar9002_initvals.h by '
              "scripts/ar9271_v2/gen_initvals.py.\n\nDo not edit by hand; re-run the generator. "
              'Each row is [reg, col1, ...]; for 2.4 GHz the\nmode column index is 4 '
              '(modesIndex), and Common is [reg, val]."""\n')
    OUT.write_text(header + "\n" + "\n\n".join(blocks) + "\n")
    print(f"wrote {OUT} ({sum(1 for _ in OUT.read_text().splitlines())} lines)")


if __name__ == "__main__":
    main()
