"""
MT7921AU post-boot bring-up sequence.

A port of the kernel's post-firmware-boot path, in the exact order the
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
from dataclasses import dataclass, field

from . import mac, mcu, txpower
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


@dataclass
class InitState:
    """Carried across the bring-up; fields fill in as blocks complete."""
    nic_capab_resp: bytes = b""
    # Per-card capability parsed from nic_capab_resp (mcu.parse_nic_capability) — the
    # runtime discriminator the band-gating (txpower) and antenna_mask (SET_RX_PATH)
    # branches read. Defaults to the reference config until the reply is parsed.
    caps: mcu.NicCaps = field(default_factory=mcu.NicCaps)


async def post_boot_init(t) -> InitState:
    """Run the full post-boot bring-up against transport ``t``.

    ``t`` exposes the unified-bus register R/W (read_reg32_unified /
    write_reg32_unified) and the async send_mcu_command — satisfied by the real
    MT7921AUTransport and by the gate's mock. The RX reader must already be
    running (the real transport's send_mcu_command waits on responses it feeds).
    """
    state = InitState()
    # The RX reader is already running (started by firmware.load_firmware /
    # driver.connect), feeding the seq-matched MCU responses these commands wait on.
    await _run_firmware_tail(t, state)
    await _init_hardware(t)
    await _regd_and_start(t, state)
    await _monitor_entry(t, state)
    return state


async def _run_firmware_tail(t, state: InitState) -> None:
    """mt7921_run_firmware after the FW download — FW_DL_EN still set.

    load_clc sits between get_nic_capability and fw_log in the kernel, but for
    this firmware/region it emits no command here (its CHIP_CONFIG lands later,
    during regd_update), so the wire is just these two commands back to back.
    """
    # GET_NIC_CAPAB (query): the reply carries the MAC address + PHY/chip caps. Parse
    # it now so the band-gating + antenna_mask branches downstream (_regd_and_start)
    # read this card's real capability, mirroring mt7921_mcu_get_nic_capability filling
    # phy->cap before __mt7921_start uses it. [SRC mt7921/mcu.c:645, init.c __mt7921_start]
    cmd, payload = mcu.get_nic_capability()
    state.nic_capab_resp = await t.send_mcu_command(cmd, payload) or b""
    state.caps = mcu.parse_nic_capability(state.nic_capab_resp)

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

    # mt76_connac_mcu_set_rate_txpower — regulatory per-rate SKU limits, one command
    # per 8-channel batch, per band the card advertises (state.caps: has_2ghz/5ghz/6ghz).
    # (The kernel's per-batch reg_rr(MT_PSE_BASE) is absent from the capture, so we omit it.)
    await _set_rate_txpower(t, state.caps)

    # mt7921_init_work tail: set_deep_sleep(ds_enable). USB leaves ds_enable=0,
    # so this is "KeepFullPwr 1" (deep sleep off).
    cmd, payload = mcu.set_deep_sleep(False)
    await t.send_mcu_command(cmd, payload, wait_resp=False)

    # __mt7921_start: radio start.
    cmd, payload = mcu.set_mac_enable(0, True)          # MAC_INIT_CTRL
    await t.send_mcu_command(cmd, payload)
    cmd, payload = mcu.set_channel_domain()             # SET_CHAN_DOMAIN (again)
    await t.send_mcu_command(cmd, payload, wait_resp=False)
    cmd, payload = mcu.set_chan_info(mcu.EXT_CMD_SET_RX_PATH, mcu.DEFAULT_CHANDEF,
                                     antenna_mask=state.caps.antenna_mask)  # SET_RX_PATH
    await t.send_mcu_command(cmd, payload)
    await _set_rate_txpower(t, state.caps)               # set_tx_sar_pwr -> txpower
    mac.reset_counters(t)                                # mt792x_mac_reset_counters


async def _set_rate_txpower(t, caps) -> None:
    for payload in txpower.rate_txpower_payloads(caps):
        cmd, p = mcu.set_rate_txpower(payload)
        await t.send_mcu_command(cmd, p, wait_resp=False)


async def _monitor_entry(t, state: InitState) -> None:
    """mt7921_add_interface for the monitor vif, then configure_filter + sniffer.
    uni_add_dev sends DEV_INFO then BSS_INFO; afterwards the reserved wcid gets a
    WTBL admission-count clear. (rxfilter / set_sniffer / config_sniffer follow.)"""
    cmd, payload = mcu.uni_dev_info(True)               # DEV_INFO_UPDATE
    await t.send_mcu_command(cmd, payload)
    cmd, payload = mcu.uni_bss_info(True)               # BSS_INFO_UPDATE
    await t.send_mcu_command(cmd, payload)
    mac.wtbl_update(t, MT792x_WTBL_RESERVED, MT_WTBL_UPDATE_ADM_COUNT_CLEAR)


async def enter_monitor(t, channel: int) -> None:
    """Operational monitor-mode entry + initial channel — the airmon equivalent.

    NOT part of the single-cursor init gate: airmon/mac80211 interleave these in a
    tool-timing-dependent order (it differs pau0f vs AXML), but each command is
    byte-verified against the capture. Sent here in the kernel's logical order.
    Requires the RX loop running so the seq-matched responses are routed back.
    """
    cmd, p = mcu.set_sniffer(True)                      # UNI SNIFFER enable
    await t.send_mcu_command(cmd, p)
    cmd, p = mcu.configure_filter()                     # SET_RX_FILTER (monitor)
    await t.send_mcu_command(cmd, p, wait_resp=False)
    cmd, p = mcu.set_bss_abort()                        # set_beacon_filter(false)
    await t.send_mcu_command(cmd, p, wait_resp=False)
    cmd, p = mcu.set_rxfilter(0, mcu.MT7921_FIF_BIT_CLR, mcu.MT_WF_RFCR_DROP_OTHER_BEACON)
    await t.send_mcu_command(cmd, p, wait_resp=False)
    cmd, p = mcu.config_sniffer(channel)                # initial channel
    await t.send_mcu_command(cmd, p, wait_resp=False)
