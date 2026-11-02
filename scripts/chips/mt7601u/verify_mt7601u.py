"""M2 gate: replay the pcap's recorded eeprom/firmware/init op stream through the port.

Strict single-cursor replay. Every op the port issues must equal the next recorded
op, in order; the first mismatch is a port bug, not a capture artifact.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_mt7601u import (  # noqa: E402
    OFF_DATA, OFF_DEV, OFF_EP, OFF_LENCAP, OFF_SETUP, OFF_TYPE, OFF_XFER, SUBMIT,
    detect_card, parse_pcapng,
)


def _mask_seq(payload: bytes) -> bytes:
    """Zero the MCU command sequence in a bulk-OUT payload."""
    from wifit3.chips.mt7601u.constants import MT_TXD_CMD_INFO_SEQ

    if len(payload) < 4:
        return payload
    info = int.from_bytes(payload[:4], "little") & ~MT_TXD_CMD_INFO_SEQ
    return info.to_bytes(4, "little") + payload[4:]


class Divergence(AssertionError):
    pass


_CTRL_KINDS = {0x07: "read", 0x02: "write", 0x42: "fce"}


class StrictReplayTransport:
    """The port's transport surface, fed the recorded ops in strict order."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self._sent_seq = 0
        self.matched = 0

    def _expect(self, kind: str, **fields) -> dict:
        if self.i >= len(self.ops):
            raise Divergence(f"port issued {kind} {fields} but the capture is exhausted "
                             f"after {self.matched} ops")
        op = self.ops[self.i]
        if op["kind"] != kind:
            raise Divergence(
                f"op #{self.i} (capture {self.matched} matched): capture has "
                f"{op['kind']}({op.get('wi', 0):#06x}) but port issued {kind}"
                + (f"({fields.get('wi', 0):#06x})" if "wi" in fields else ""))
        for key, want in fields.items():
            got = op.get(key)
            if got != want:
                raise Divergence(
                    f"op #{self.i} ({kind}): {key} port={want!r} capture={got!r}")
        self.i += 1
        self.matched += 1
        return op

    # -- transport surface used by the port -------------------------------
    def rr(self, offset: int) -> int:
        op = self._expect("read", wi=offset)
        return int.from_bytes(op["data"], "little") if len(op["data"]) == 4 else 0xFFFFFFFF

    def wr(self, offset: int, val: int) -> None:
        self._expect("write", wi=offset, wv=val & 0xFFFF)
        self._expect("write", wi=offset + 2, wv=(val >> 16) & 0xFFFF)

    def fce_wr(self, offset: int, val: int) -> None:
        self._expect("fce", wi=offset, wv=val & 0xFFFF)
        self._expect("fce", wi=offset + 2, wv=(val >> 16) & 0xFFFF)

    def vendor_request(self, req: int, direction: int, val: int, offset: int,
                       data=None, buflen: int = 0) -> int:
        """One raw vendor control transfer, checked in strict order.

        The IVB handoff (mcu.c:385) is MT_VEND_DEV_MODE with wValue 0x12 and the
        64-byte IVB as the OUT payload; replaying it proves the boot vector the
        port hands over is the one the kernel sent.
        """
        kind = "ivb" if val == 0x12 else "devmode"
        op = self._expect(kind, wv=val)
        if kind == "ivb" and data is not None:
            if op["payload"] != data:
                raise Divergence(
                    f"op #{self.i} (IVB): port sent {len(data)}B "
                    f"{data[:16].hex()}\n           capture had {len(op['payload'])}B "
                    f"{op['payload'][:16].hex()}")
        return len(data) if data is not None else buflen

    def rmw(self, offset: int, mask: int, val: int) -> int:
        """Read-modify-write that always writes (usb.c:188 mt7601u_rmw)."""
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def reset(self) -> None:
        """mt7601u_vendor_reset: MT_VEND_DEV_MODE / MT_VEND_DEV_MODE_RESET."""
        self._expect("devmode", wv=0x0001)

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        op = self._expect("bulk_out")
        from wifit3.chips.mt7601u import constants as C
        self._sent_seq = C._field_get(
            C.MT_TXD_CMD_INFO_SEQ, int.from_bytes(data[:4], "little"))
        # The MCU command seq is a free-running session counter, not port output: a
        # replay that starts mid-session can never match a recording's counter.
        # Everything else is compared byte-for-byte.
        if _mask_seq(op["data"]) != _mask_seq(data):
            raise Divergence(
                f"op #{self.i} (bulk_out): port sent {len(data)}B "
                f"{data[:32].hex()}\n           capture had {len(op['data'])}B "
                f"{op['data'][:32].hex()}")
        return len(data)

    def bulk_out_inband_fw(self, data: bytes, timeout_ms: int = 0) -> int:
        """The firmware-download URB (mcu.c:311) shares the inband endpoint."""
        return self.bulk_out(data, timeout_ms)

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        """A CMD_DONE echoing the seq just sent.

        Only the host's requests are gated here; the MCU's replies are 16-byte
        CMD_DONE words with evt 0 and no payload the driver branches on, so this
        satisfies the await rather than byte-comparing it.
        """
        from wifit3.chips.mt7601u import constants as C

        info = (C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, self._sent_seq)
                | C._field_prep(C.MT_RXD_CMD_INFO_EVT_TYPE, C.CMD_DONE))
        return info.to_bytes(4, "little")


def load_ops(pcap: str, dev: int) -> list[dict]:
    """Recorded vendor ops for one device, submit-only, with IN data attached."""
    pkts = parse_pcapng(pcap)
    ops = []
    for i, pkt in enumerate(pkts):
        if len(pkt) < 48 or pkt[OFF_DEV] != dev:
            continue
        if pkt[OFF_XFER] == 2 and pkt[OFF_TYPE] == SUBMIT:
            bm, br, wv, wi, _wl = struct.unpack_from("<BBHHH", pkt, OFF_SETUP)
            if (bm & 0x60) != 0x40:
                continue
            if br == 0x01:
                # MT_VEND_DEV_MODE: the vendor_reset at mcu.c:462 (wValue 1) and the
                # IVB handoff at mcu.c:385 (wValue 0x12, 64-byte OUT payload).
                wl = struct.unpack_from("<BBHHH", pkt, OFF_SETUP)[4]
                payload = bytes(pkt[OFF_DATA:OFF_DATA + wl]) if wl else b""
                ops.append({"kind": "ivb" if wv == 0x12 else "devmode",
                            "wi": wi, "wv": wv, "data": b"", "payload": payload})
                continue
            if br not in _CTRL_KINDS:
                continue
            data = b""
            if br == 0x07:
                data = bytes(pkts[i + 1][OFF_DATA:OFF_DATA + 4])
            ops.append({"kind": _CTRL_KINDS[br],
                        "wi": wi, "wv": wv, "data": data, "raw_br": br})
        elif pkt[OFF_XFER] == 3 and pkt[OFF_EP] == 0x08 and pkt[OFF_TYPE] == SUBMIT:
            lencap = struct.unpack_from("<I", pkt, OFF_LENCAP)[0]
            ops.append({"kind": "bulk_out", "wi": None, "wv": None,
                        "data": pkt[OFF_DATA:OFF_DATA + lencap], "raw_br": None})
    return ops


def main() -> int:
    pcap = sys.argv[1]
    stage = sys.argv[2] if len(sys.argv) > 2 else "eeprom"
    pkts = parse_pcapng(pcap)
    dev = detect_card(pkts)
    ops = load_ops(pcap, dev)
    print(f"dev {dev}: {len(ops)} recorded ops")

    from wifit3.chips.mt7601u.eeprom import MT7601UEeprom

    if stage == "eeprom":
        # eeprom_init starts at the first efuse KICK write. The probe also reads
        # MT_EFUSE_CTRL (usb.c:304) but never writes it, so the first 0x0024 write
        # is unambiguous. MT_EFUSE_CTRL_KICK is bit 30, which lands in the high
        # half (0x0026) because every write is split into two transfers.
        first_write = next(i for i, o in enumerate(ops)
                           if o["kind"] == "write" and o["wi"] == 0x0024)
        start = first_write - 1      # efuse_read reads MT_EFUSE_CTRL before writing it
        tp = StrictReplayTransport(ops)
        tp.i = start
        ee = MT7601UEeprom(tp)
        # read() stops once the EEPROM bytes are decoded; writing MT_MAC_ADDR_DW0 is
        # mac.c's set_macaddr, the next bring-up stage. Everything up to that write is
        # the efuse path: the usage-map size check plus all 256 EEPROM bytes.
        # eeprom_init ends where phy_init begins: MT_TX_PWR_CFG_9 (0x13dc) is the last
        # register the EEPROM decoder writes (_extra_power_over_mac, eeprom.c:243-246);
        # the read of 0x121c after it belongs to phy.c's init.
        eeprom_end = next(i for i, o in enumerate(ops)
                          if o["kind"] == "read" and o["wi"] == 0x121C)
        try:
            ee.read()
        except Divergence as e:
            print(f"DIVERGED after {tp.matched} matched ops (cursor {tp.i}):\n  {e}")
            return 1
        if tp.i != eeprom_end:
            print(f"DIVERGED: decode ended at op #{tp.i}, expected #{eeprom_end}")
            return 1
        print(f"MATCH: efuse read + decode reproduced {tp.matched} ops exactly")
        print("  usage-map size check, all 256 EEPROM bytes, MAC, per-rate TX power")
        print(f"  matched ops #{start}..{tp.i - 1}; next stage (phy_init) starts at #{tp.i}")
        return 0

    if stage == "firmware":
        return verify_firmware(ops, start_of(ops), dev)

    if stage == "wcid":
        return verify_wcid(ops)
    print(f"stage {stage!r} not wired yet", file=sys.stderr)
    return 2


def wcid_start(ops: list[dict]) -> int:
    """The first CMD_BURST_WRITE to the WCID address table.

    init.c runs init_wcid_mem, init_key_mem and init_wcid_attr_mem in that order,
    so the WCID table is the unambiguous opener.
    """
    from wifit3.chips.mt7601u import constants as C
    want = C.MT_MCU_MEMMAP_WLAN + C.MT_WCID_ADDR_BASE
    return next(i for i, o in enumerate(ops) if is_burst_to(o, want))


def burst_addr(op: dict) -> int | None:
    """The destination address of a CMD_BURST_WRITE op, or None for anything else."""
    from wifit3.chips.mt7601u import constants as C
    if op["kind"] != "bulk_out" or len(op["data"]) < 8:
        return None
    info = int.from_bytes(op["data"][:4], "little")
    if C._field_get(C.MT_TXD_CMD_INFO_TYPE, info) != C.CMD_BURST_WRITE:
        return None
    return int.from_bytes(op["data"][4:8], "little")


def is_burst_to(op: dict, addr: int) -> bool:
    """True when ``op`` is a CMD_BURST_WRITE naming ``addr`` as its destination."""
    return burst_addr(op) == addr


def last_burst_in(ops: list[dict], base: int, words: int) -> int:
    """Index of the last chunk of the burst covering ``base..base+words``.

    Only the first chunk names the base; the rest advance by (LEN-1) words, so
    matching on the base alone finds one op and truncates the stage.
    """
    from wifit3.chips.mt7601u import constants as C
    lo = C.MT_MCU_MEMMAP_WLAN + base
    hi = lo + words * 4
    return max(i for i, o in enumerate(ops) if (a := burst_addr(o)) is not None and lo <= a < hi)


def verify_wcid(ops: list[dict]) -> int:
    """Replay the three station-memory clears against the capture, strictly."""
    from wifit3.chips.mt7601u import constants as C
    from wifit3.chips.mt7601u.mcu import MT7601UMcu
    from wifit3.chips.mt7601u.wcid import init_station_memory

    # WCID_ATTR is the last of the three, so the stage ends on its last chunk.
    last = last_burst_in(ops, C.MT_WCID_ATTR_BASE, 256)
    tp = StrictReplayTransport(ops)
    tp.i = wcid_start(ops)
    mcu = MT7601UMcu(tp)
    # These clears run after the firmware is resident (init.c:351), so the MCU is
    # already running by this point in the capture.
    mcu.mcu_running = True
    try:
        init_station_memory(mcu)
    except Divergence as e:
        print(f"DIVERGED after {tp.matched} matched ops (cursor {tp.i}):\n  {e}")
        return 1
    except Exception as e:
        print(f"port raised {type(e).__name__} at op #{tp.i} "
              f"({tp.matched} matched):\n  {e}")
        return 1
    if tp.i != last + 1:
        print(f"DIVERGED: clears ended at op #{tp.i}, expected #{last + 1}")
        return 1
    print(f"MATCH: WCID, key and WCID-attribute clears reproduced "
          f"{tp.matched} ops exactly")
    print(f"  matched ops #{wcid_start(ops)}..{tp.i - 1}; "
          f"write_mac_initvals starts at #{tp.i}")
    return 0


def start_of(ops: list[dict]) -> int:
    """mt7601u_load_firmware's first act: enable the bulk pipes (mcu.c:419)."""
    return next(i for i, o in enumerate(ops)
                if o["kind"] == "write" and o["wi"] == 0x0238)


def verify_firmware(ops: list[dict], start: int, dev: int) -> int:
    """Replay mt7601u_load_firmware against the capture, strictly."""
    from wifit3.chips.mt7601u import firmware as fw_mod
    from wifit3.chips.mt7601u.mcu import MT7601UMcu

    # The firmware image lands in the four big bulk-OUT chunks; MCU inband commands
    # share that endpoint, so size is what separates them. The stage ends once
    # the MCU reports running: MT_WPDMA_GLO_CFG is the first op of the next
    # bring-up stage (mt7601u_init_hardware's post-FW DMA poll, init.c:339).
    chunks = [i for i, o in enumerate(ops)
              if o["kind"] == "bulk_out" and len(o["data"]) > 1000]
    end = next(i for i, o in enumerate(ops)
               if i > chunks[-1] and o["kind"] == "read" and o["wi"] == 0x0208)
    tp = StrictReplayTransport(ops)
    tp.i = start
    mcu = MT7601UMcu(tp)
    try:
        fw_mod.load_firmware(tp, mcu)
    except Divergence as e:
        print(f"DIVERGED after {tp.matched} matched ops (cursor {tp.i}):\n  {e}")
        return 1
    except Exception as e:
        print(f"port raised {type(e).__name__} at op #{tp.i} "
              f"({tp.matched} matched):\n  {e}")
        return 1
    if tp.i != end:
        print(f"DIVERGED: download ended at op #{tp.i}, expected #{end} "
              f"(the MT_WPDMA_GLO_CFG poll of the next bring-up stage)")
        return 1
    print(f"MATCH: firmware preamble + ILM download + IVB handoff reproduced "
          f"{tp.matched} ops exactly")
    print(f"  matched ops #{start}..{tp.i - 1}; init_hardware starts at #{tp.i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())