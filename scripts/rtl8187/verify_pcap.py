"""Acceptance gate: replay-diff the rtl8187 (RTL8187L, ALFA AWUS036H) MAC bring-up against
its cold-boot capture.

The 8187L is the oldest Realtek USB part -- hard-MAC, no firmware -- but its register wire
is the same vendor 0x05 (address in wValue), so it reuses the shared Realtek replay engine.
Bring-up is pure control transfers; this gate replays the port's ``mac.init_hw`` against the
chip's recorded reads and checks every write byte-for-byte.

Coverage grows with the port -- currently M2a: ``init_hw``'s MAC sequence up to the rtl8225
RF synth (set_anaparam x2, the 0xFE18 soft-reset toggle, cmd_reset's poll, the RF-pin/CONFIG
setup, host_usb_init, and the RF_TIMING/RF_PARA/CONFIG3 block). A sentinel rf_init stops the
diff exactly where init_hw hands off to the external transceiver.

Not yet gated (own milestones): the rtl8225 RF synth (rtl8225_write_8051 drives
t.dev.ctrl_transfer with wIndex=0x8225, so it needs the fake-dev replay, not this
reimplementation transport), and start()'s RX_CONF -- which is the deliberate monitor-mode
deviation from the kernel's station filter, so it is expected to differ on the wire.

Run: uv run python scripts/rtl8187/verify_pcap.py [capture-1]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8187 import mac  # noqa: E402
from wifit3.chips.rtl8187.constants import (  # noqa: E402
    ANAPARAM_ON,
    EEPROM_CMD_CONFIG,
    REG_ANAPARAM,
    REG_EEPROM_CMD,
)

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8187"
_WHOLE = (1, 10 ** 9)


class _StopAtRf(Exception):
    """Raised by the sentinel rf_init to halt the replay where init_hw hands off to the
    rtl8225 synth (the RF programming is a separate, fake-dev milestone)."""


def _sentinel_rf(_t) -> None:
    raise _StopAtRf()


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev, _WHOLE)

    # init_hw opens with set_anaparam's write8(EEPROM_CMD, CONFIG). EEPROM_CMD is also
    # written during the RF-setup probe, so anchor on the first ANAPARAM=ANAPARAM_ON write
    # (unique to set_anaparam) and back up to the EEPROM_CMD(CONFIG) that opens it.
    ana = next((i for i, o in enumerate(ops)
                if o["kind"] == "W" and o["addr"] == REG_ANAPARAM and o["value"] == ANAPARAM_ON),
               None)
    if ana is None:
        print("FAIL: no ANAPARAM=ANAPARAM_ON write found (set_anaparam never ran?)")
        return 1
    start = next((i for i in range(ana, -1, -1)
                  if ops[i]["kind"] == "W" and ops[i]["addr"] == REG_EEPROM_CMD
                  and ops[i]["value"] == EEPROM_CMD_CONFIG), None)
    if start is None:
        print("FAIL: no EEPROM_CMD(CONFIG) precedes the first ANAPARAM write")
        return 1
    print(f"{name}: card=dev{dev}, {len(ops)} vendor ops, init_hw anchored at op {start} "
          f"(frame {ops[start]['frame']})")

    t = rp.ReplayTransport(ops[start:])
    try:
        mac.init_hw(t, rf_init=_sentinel_rf)
    except _StopAtRf:
        pass
    except rp.Divergence as e:
        print(f"\nFAIL (divergence):\n  {e}")
        print(f"  reproduced {t.i} ops before diverging")
        return 1

    print(f"\nPASS: reproduced init_hw's MAC bring-up in {t.i} ops byte-for-byte -- "
          f"set_anaparam x2, 0xFE18 soft-reset, cmd_reset poll, RF-pin + CONFIG1 + "
          f"host_usb_init + RF_TIMING/RF_PARA/CONFIG3 -- up to the rtl8225 RF synth.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
