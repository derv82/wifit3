"""
MT7921AU post-boot bring-up sequence.

A faithful port of the kernel's post-firmware-boot path, in the exact order the
cold-boot capture records it. The boundary with firmware.py is mt792x_load_firmware:
firmware.load_firmware() ends at the FW_N9_RDY poll with FW_DL_EN still set; this
module takes over at mt7921_run_firmware's tail.

  mt7921_run_firmware (tail)  -> get_nic_capability, fw_log_2_host
  mt7921u_mcu_init            -> clear FW_DL_EN
  __mt7921_init_hardware      -> set_eeprom, mac_init (+ rts_thresh)
  ... regd_update, monitor entry, channel — ported incrementally below

The single-cursor CHECK 3 gate (scripts/mt7921au/verify_pcap.py) drives this
against the captured wire; every op here reproduces the capture byte-for-byte.
"""
import logging
from dataclasses import dataclass

from . import mac, mcu, txpower
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


@dataclass
class InitState:
    """Carried across the bring-up; fields fill in as blocks complete."""
    nic_capab_resp: bytes = b""


async def post_boot_init(t) -> InitState:
    """Run the full post-boot bring-up against transport ``t``.

    ``t`` exposes the unified-bus register R/W (read_reg32_unified /
    write_reg32_unified), the async send_mcu_command, and the MCU drainer
    lifecycle — satisfied by the real MT7921AUTransport and by the gate's mock.
    """
    state = InitState()
    await t.start_mcu_drainer()
    try:
        await _run_firmware_tail(t, state)
        await _init_hardware(t)
        await _regd_and_start(t, state)
    finally:
        await t.stop_mcu_drainer()
    return state


async def _run_firmware_tail(t, state: InitState) -> None:
    """mt7921_run_firmware after the FW download — FW_DL_EN still set.

    load_clc sits between get_nic_capability and fw_log in the kernel, but for
    this firmware/region it emits no command here (its CHIP_CONFIG lands later,
    during regd_update), so the wire is just these two commands back to back.
    """
    # GET_NIC_CAPAB (query): the reply carries the MAC address + PHY/chip caps,
    # parsed when the monitor entry (DEV_INFO) needs them.
    cmd, payload = mcu.get_nic_capability()
    state.nic_capab_resp = await t.send_mcu_command(cmd, payload) or b""

    cmd, payload = mcu.fw_log_2_host(1)
    await t.send_mcu_command(cmd, payload, wait_resp=False)


async def _init_hardware(t) -> None:
    """mt7921u_mcu_init tail + __mt7921_init_hardware."""
    # mt7921u_mcu_init: mt76_clear(MT_UDMA_TX_QSEL, FW_DL_EN) once the FW is up.
    mac.clear_bits(t, MT_UDMA_TX_QSEL, MT_FW_DL_EN)

    # __mt7921_init_hardware: set_eeprom then mac_init.
    cmd, payload = mcu.set_eeprom()
    await t.send_mcu_command(cmd, payload)

    mac.mac_init(t)
    # mt7921_mac_init's trailing rts_thresh is an MCU command (PROTECT_CTRL).
    cmd, payload = mcu.set_rts_thresh(MT_RTS_THRESH_DEFAULT, 0)
    await t.send_mcu_command(cmd, payload)


async def _regd_and_start(t, state: InitState) -> None:
    """Regulatory + radio-start configuration, in the wire order the capture
    records after mac_init (see MT7921AU.md): channel domain, TX-power SKU,
    CLC, MAC-init-ctrl, channel domain again, RX path, TX-power SKU, then the
    monitor entry. Ported incrementally; the gate names the next op."""
    # mt76_connac_mcu_set_channel_domain (world '00' domain).
    cmd, payload = mcu.set_channel_domain()
    await t.send_mcu_command(cmd, payload, wait_resp=False)

    # mt76_connac_mcu_set_rate_txpower — regulatory per-rate SKU limits, one
    # command per 8-channel batch across 2.4/5/6 GHz. (The kernel's per-batch
    # reg_rr(MT_PSE_BASE) is absent from the capture, so we omit it.)
    await _set_rate_txpower(t)


async def _set_rate_txpower(t) -> None:
    for payload in txpower.rate_txpower_payloads():
        cmd, p = mcu.set_rate_txpower(payload)
        await t.send_mcu_command(cmd, p, wait_resp=False)
