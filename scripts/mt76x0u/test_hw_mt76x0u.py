"""Hardware test for MT76x0U / MT7610U bring-up.

  --phase probe : USB enumeration + claim + MAC_CSR0 read (M0).
  --phase fw    : probe + FW upload + FW_READY ack (M1).
  --phase mcu   : fw + MCU CMD_RANDOM_READ round-trip + EFUSE summary (M2).
  --phase mac    : mcu + init_mac_registers + wait_for_txrx_idle (M3a).
  --phase bbp    : mac + init_bbp (M3b).
  --phase eeprom : bbp + full EEPROM init + mac_setaddr (M3c).
  --phase ant    : eeprom + phy_ant_select (M3d.1).
  --phase phy    : ant + phy_rf_init + set_rxpath + set_txdac (M3d.2 → full M3 done).
  --phase all    : runs the latest milestone.

Usage:
    uv run python scripts/mt76x0u/test_hw_mt76x0u.py --phase probe
    uv run python scripts/mt76x0u/test_hw_mt76x0u.py --phase fw [--debug]

Per CLAUDE.md "Hardware testing is the USER's job" — this script is run by
the operator. Output paste-back drives the next iteration.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.mt76x0u.constants import (
    MT_MAC_CSR0,
    MT_MCU_COM_REG0,
    MT_MCU_COM_REG0_FW_READY,
    USB_IDS_MT76X0U,
)
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.engine.protocols import DeviceID


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(label: str) -> None:
    print(f"\n--- {label} ---")


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[ ..  ] {msg}")


def open_device():
    backend = libusb_package.get_libusb1_backend()
    for vid, pid, desc in USB_IDS_MT76X0U:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            print(f"[*] Matched device: {desc} ({vid:04x}:{pid:04x})")
            return found, DeviceID(vid, pid, desc)
    fail(
        "No supported MT76x0U device found. Expected one of: "
        + ", ".join(f"{v:04x}:{p:04x}" for v, p, _ in USB_IDS_MT76X0U)
    )


def progress(pct: float, msg: str) -> None:
    print(f"  [{pct * 100:5.1f}%] {msg}")


async def phase_probe(driver: MT76x0UDriver) -> None:
    """M0 — claim interface, read MAC_CSR0 + MT_MCU_COM_REG0.

    Read-only against the chip. Safe on warm AND cold boot.
    """
    step("M0 — interface claim + register read")
    try:
        driver.transport.claim()
    except RuntimeError as e:
        fail(f"interface claim: {e}")
    ok("interface claimed")

    try:
        mac_csr0 = driver.transport.read32(MT_MAC_CSR0)
    except Exception as e:
        fail(f"MAC_CSR0 read: {type(e).__name__}: {e}")
    if mac_csr0 in (0, 0xFFFFFFFF):
        info(f"MAC_CSR0 = 0x{mac_csr0:08x} (chip not yet powered — expected on cold boot)")
    else:
        ok(f"MAC_CSR0 = 0x{mac_csr0:08x} (chip is alive — likely warm)")

    try:
        com_reg0 = driver.transport.read32(MT_MCU_COM_REG0)
    except Exception as e:
        fail(f"MT_MCU_COM_REG0 read: {type(e).__name__}: {e}")
    fw_running = bool(com_reg0 & MT_MCU_COM_REG0_FW_READY)
    ok(
        f"MT_MCU_COM_REG0 = 0x{com_reg0:08x}  "
        f"(FW_READY {'set — warm chip with FW already running' if fw_running else 'clear — cold boot, FW upload needed'})"
    )


async def phase_fw(driver: MT76x0UDriver) -> None:
    """M1+M2 — full bring-up via driver.connect(): chip on + FW upload +
    FW_READY ack + MCU smoke + EFUSE summary."""
    step("M1+M2 — firmware + MCU + EFUSE bring-up")
    success = await driver.connect(progress_cb=progress)
    if not success:
        fail("driver.connect() returned False — see logs above")
    if driver.fw_info is None:
        fail("driver.connect() succeeded but fw_info is None")
    h = driver.fw_info.get("header")
    if driver.fw_info["skipped"]:
        info("Warm boot — FW was already running (no upload needed)")
    else:
        ok(f"FW version: {h['fw_ver_str']}  build 0x{h['build_ver']:04x}  time {h['build_time']!r}")
        ok(f"FW_READY ack after {driver.fw_info['polls']} poll(s)")


async def phase_phy(driver: MT76x0UDriver) -> None:
    """M3d.2 — confirm full phy_init landed (RF init + rxpath + txdac)."""
    step("M3d.2 — phy_init assertions")
    if driver.bbp_agc0_after_phy is None:
        fail("MT_BBP(AGC, 0) post-phy not populated")
    if driver.bbp_txbe5_after_phy is None:
        fail("MT_BBP(TXBE, 5) post-phy not populated")
    agc0 = driver.bbp_agc0_after_phy
    txbe5 = driver.bbp_txbe5_after_phy
    # set_rxpath for 1T1R clears BIT(3) and BIT(4) of AGC(0).
    if agc0 & ((1 << 3) | (1 << 4)):
        fail(f"MT_BBP(AGC, 0)=0x{agc0:08x} — BIT(3) or BIT(4) still set "
             f"(set_rxpath should have cleared both for 1T1R)")
    ok(f"MT_BBP(AGC, 0) post-phy = 0x{agc0:08x} (BIT 3|4 cleared → single-stream RX)")
    # set_txdac for 1T1R clears bits 0+1 of TXBE(5).
    if txbe5 & 0x3:
        fail(f"MT_BBP(TXBE, 5)=0x{txbe5:08x} — bits 0/1 still set "
             f"(set_txdac should have cleared for 1T1R)")
    ok(f"MT_BBP(TXBE, 5) post-phy = 0x{txbe5:08x} (bits 0/1 cleared → single-stream TX)")
    if driver.rf_b0_r22_after_phy is not None:
        # The freq cal writes min(efuse_full.freq_offset & 0xff, 0xbf) → MT_RF(0, 22).
        expected = min(driver.efuse_full.freq_offset & 0xFF, 0xBF)
        if driver.rf_b0_r22_after_phy != expected:
            fail(f"MT_RF(0, 22) readback 0x{driver.rf_b0_r22_after_phy:02x} != "
                 f"expected 0x{expected:02x} (freq_offset={driver.efuse_full.freq_offset})")
        ok(f"MT_RF(0, 22) freq cal = 0x{driver.rf_b0_r22_after_phy:02x} "
           f"(matches min(freq_offset, 0xbf) = 0x{expected:02x})")
    else:
        info("MT_RF(0, 22) readback skipped (MCU read failed earlier)")
    ok("M3d.2 complete — full M3 done")


async def phase_ant(driver: MT76x0UDriver) -> None:
    """M3d.1 — confirm phy_ant_select landed."""
    step("M3d.1 — phy_ant_select assertions")
    if driver.wlan_fun_ctrl_after_ant is None or driver.coexcfg3_after_ant is None:
        fail("ant_select post-state not populated")
    wlan = driver.wlan_fun_ctrl_after_ant
    coex3 = driver.coexcfg3_after_ant
    # Kernel guarantees: WLAN_FUN_CTRL bit 5 (FRC_WL_ANT_SEL) is cleared.
    if wlan & (1 << 5):
        fail(f"WLAN_FUN_CTRL=0x{wlan:08x} has FRC_WL_ANT_SEL (BIT 5) set "
             f"(should be cleared by ant_select)")
    ok(f"WLAN_FUN_CTRL post-ant = 0x{wlan:08x} (FRC_WL_ANT_SEL cleared)")
    # COEXCFG3: bits 2-5 are cleared, then per-mode bits set. For dual-band
    # single-antenna we expect BIT(3)|BIT(4) set.
    if (coex3 & 0x18) != 0x18:
        info(f"COEXCFG3=0x{coex3:08x} — BIT(3)|BIT(4) not both set; mode "
             f"may differ from single-antenna 2.4+5GHz (check log)")
    else:
        ok(f"COEXCFG3 post-ant = 0x{coex3:08x} (BIT(3)|BIT(4) set — "
           f"single antenna, dual-band)")
    ok("M3d.1 complete")


async def phase_eeprom(driver: MT76x0UDriver) -> None:
    """M3c — confirm full EEPROM init + mac_setaddr landed."""
    step("M3c — EEPROM init + mac_setaddr assertions")
    e = driver.efuse_full
    if e is None:
        fail("efuse_full not populated — was connect() called?")
    if e.chip_id not in (0x7610, 0x7650):
        fail(f"chip_id 0x{e.chip_id:04x} unexpected (want 0x7610 or 0x7650)")
    ok(f"EEPROM chip_id = 0x{e.chip_id:04x}, ver=0x{e.version:02x}, fae=0x{e.fae:02x}")
    if e.mac_bytes == b"\xff\xff\xff\xff\xff\xff" or e.mac_bytes == b"\x00" * 6:
        fail(f"MAC address invalid: {e.mac_address}")
    ok(f"EFUSE MAC: {e.mac_address}")
    ok(f"chip cap: tx={e.tx_path} rx={e.rx_path}  "
       f"2.4G={e.has_2ghz} 5G={e.has_5ghz}")
    if e.cap_warnings:
        for w in e.cap_warnings:
            info(f"cap warning: {w}")
    ok(f"freq_offset={e.freq_offset}  temp_offset={e.temp_offset}")
    if driver.rxfilter_default is None:
        fail("rxfilter_default not populated")
    ok(f"RX_FILTR_CFG default = 0x{driver.rxfilter_default:08x}")

    # Verify mac_setaddr writeback: read MT_MAC_ADDR_DW0 and confirm the
    # low 4 MAC bytes round-trip.
    from wifit3.chips.mt76x0u.constants import MT_MAC_ADDR_DW0
    mac_dw0 = driver.transport.read32(MT_MAC_ADDR_DW0)
    expected_dw0 = int.from_bytes(e.mac_bytes[:4], "little")
    if mac_dw0 != expected_dw0:
        fail(f"MAC_ADDR_DW0 readback 0x{mac_dw0:08x} != expected 0x{expected_dw0:08x}")
    ok(f"MAC_ADDR_DW0 readback OK (0x{mac_dw0:08x} = first 4 bytes of MAC)")
    ok("M3c complete")


async def phase_bbp(driver: MT76x0UDriver) -> None:
    """M3b — confirm BBP init landed and BBP version is sensible."""
    step("M3b — BBP init assertions")
    if driver.bbp_version is None:
        fail("BBP version not populated — was connect() called?")
    v = driver.bbp_version
    if v == 0 or v == 0xFFFFFFFF:
        fail(f"BBP version 0x{v:08x} is 0 or all-1s (BBP not ready or stalled)")
    ok(f"BBP version = 0x{v:08x} (not 0, not all-1s)")
    ok("M3b complete")


async def phase_mac(driver: MT76x0UDriver) -> None:
    """M3a — confirm MAC init landed and TX|RX is idle."""
    step("M3a — MAC init assertions")
    if driver.mac_status_after_init is None:
        fail("MAC_STATUS post-init not populated — was connect() called?")
    mac_status = driver.mac_status_after_init
    tx_bit = mac_status & 0x1
    rx_bit = (mac_status >> 1) & 0x1
    if tx_bit or rx_bit:
        fail(f"MAC_STATUS=0x{mac_status:08x} — TX or RX bit still set "
             f"(TX={tx_bit} RX={rx_bit})")
    ok(f"MAC_STATUS post-init = 0x{mac_status:08x} (TX|RX = 0, idle)")
    ok("M3a complete")


async def phase_mcu(driver: MT76x0UDriver) -> None:
    """M2 — confirm MCU smoke landed. (EFUSE moved to phase_eeprom in M3c.)"""
    step("M2 — MCU assertions")
    if driver.mcu_smoke is None:
        fail("MCU smoke result not populated — was connect() called?")
    if not driver.mcu_smoke["match"]:
        fail(
            f"MCU smoke mismatch: direct=0x{driver.mcu_smoke['via_vendor_read']:08x} "
            f"vs MCU=0x{driver.mcu_smoke['via_mcu_read']:08x}"
        )
    ok(
        f"MCU CMD_RANDOM_READ round-trip OK "
        f"(MAC_CSR0 via MCU = 0x{driver.mcu_smoke['via_mcu_read']:08x}, "
        f"matches direct read)"
    )
    ok("M2 complete")


def main() -> int:
    parser = argparse.ArgumentParser(description="MT76x0U hardware test driver")
    parser.add_argument(
        "--phase",
        choices=["probe", "fw", "mcu", "mac", "bbp", "eeprom", "ant", "phy", "all"],
        default="all",
        help="Which milestone to test (default: all)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    dev, id_entry = open_device()
    driver = MT76x0UDriver.from_usb_device(dev, id_entry)

    try:
        if args.phase in ("probe", "all"):
            asyncio.run(phase_probe(driver))
            # If we're running "all", we want the chip in a known state for
            # the next phase. Probe is read-only so we just continue.
        if args.phase in ("fw", "mcu", "mac", "bbp", "eeprom", "ant", "phy", "all"):
            # driver.connect() runs M1+M2+M3a+M3b+M3c+M3d together.
            asyncio.run(phase_fw(driver))
        if args.phase in ("mcu", "mac", "bbp", "eeprom", "ant", "phy", "all"):
            asyncio.run(phase_mcu(driver))
        if args.phase in ("mac", "bbp", "eeprom", "ant", "phy", "all"):
            asyncio.run(phase_mac(driver))
        if args.phase in ("bbp", "eeprom", "ant", "phy", "all"):
            asyncio.run(phase_bbp(driver))
        if args.phase in ("eeprom", "ant", "phy", "all"):
            asyncio.run(phase_eeprom(driver))
        if args.phase in ("ant", "phy", "all"):
            asyncio.run(phase_ant(driver))
        if args.phase in ("phy", "all"):
            asyncio.run(phase_phy(driver))
        return 0
    finally:
        try:
            asyncio.run(driver.close())
        except Exception as e:
            print(f"[WARN] driver.close() raised: {e}")


if __name__ == "__main__":
    sys.exit(main())
